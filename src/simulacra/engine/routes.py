"""Retrieval routes: what a conversation prompt is allowed to contain.

Before M8 there was one retrieval channel. *"What's down that corridor"* and
*"how did the last delver die"* went through identical machinery -- embed the
question, nearest-neighbour it against memories tagged with this NPC -- so the
answer to the first was a vector search over deaths.

The fix is not a better embedding. It is noticing that **you do not run a
semantic search to answer a question the engine already knows the answer to.**
The player's topic classifies into a named route; each route has its own
resolver; and only one of the seven touches the vector index. Prompts shrink
because each carries what its own route resolved, instead of three memories
picked by topical proximity to a sentence.

Two things fall out of this for free, which is how you can tell the shape is
right:

* **"I don't know" needs no threshold.** A resolver that finds nothing returns
  `[]`, and an empty list is a truth about the data. The cancelled T2 would have
  spent a milestone calibrating a recall-distance cutoff against a live
  embedding model, plus a guard against an embedding outage reading as maximal
  relevance.
* **The instruction can be per-route.** M4's unconditional "say what happened to
  the delver you remember" was correct for the only route that existed. It is
  the `past` route's instruction, and always was.

MILESTONE M8.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .dealings import holdings, is_warm, is_wary

# Order is the order they are tried in; `unknown` is the honest fallback.
ROUTES = ("self", "room", "monsters", "npc", "item", "trade", "past", "unknown")

# What an NPC reaches for when the player gives it nothing to go on. Phrased
# about outcomes, not conversations: "who spoke with X" ranked the NPC's own
# past conversations above an actual death, because those memories are literally
# about speaking. Arrived at the hard way in M4; do not reword casually.
DEATH_QUERY = "How did the previous delver die, and on which floor?"

# Two or three recollections, never more. Prompt bloat is real at 4096 context,
# and a small model handed six memories recites a list instead of speaking.
RECALL_LIMIT = 3
CANON_LIMIT = 4
TOLD_LIMIT = 2
WORLD_LIMIT = 5

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {"route": {"type": "string", "enum": list(ROUTES)}},
    "required": ["route"],
}

# Stage-1 keywords. Costs nothing, and its hit rate is the number that decides
# whether the two-stage design pays for itself (M8 R8).
_KEYWORDS: dict[str, set[str]] = {
    "room": {"room", "here", "place", "door", "corridor", "exit", "exits", "way",
             "wall", "walls", "floor", "stair", "stairs", "hall", "chamber"},
    "monsters": {"monster", "monsters", "thing", "things", "creature", "danger",
                 "dangerous", "safe", "hostile", "ahead", "nearby", "close"},
    "item": {"item", "items", "key", "blade", "knife", "water", "ration", "jar",
             "seal", "inventory", "weapon"},
    "trade": {"trade", "carry", "carrying", "hold", "holding", "spare", "sell",
              "barter", "have"},
    "self": {"you", "your", "yours", "yourself", "name", "who"},
    "past": {"delver", "delvers", "died", "die", "death", "dead", "killed",
             "before", "last", "happened", "others", "previous"},
}

# Per-route instruction. Every branch has one: M7 learned the expensive way that
# a prompt of pure data makes a 1.7b transcribe the last thing it was handed.
_INSTRUCTIONS = {
    # M13: "say out loud what happened" was obeyed as narration -- "The delver
    # approached the Assayer on floor 1..." -- a report about the NPC, in the
    # NPC's mouth. Told who it is talking to and in which person, it speaks.
    "past": ("Speak to the delver in front of you, as yourself: tell them what "
             "happened to the one you remember -- the floor, and what killed "
             "them. Say \"I\" for yourself; do not narrate. Do not invent any "
             "other history."),
    "self": "Answer them from what is true of you. Do not invent history.",
    "none": "You do not know anything about that. Say so briefly, in character.",
    "repeat": ("You have already told them this. Say so, briefly, and do not "
               "repeat it."),
    "facts": "Answer using only what is listed. Do not invent anything else.",
}

# Said in full, without asking the model anything. Measured on a live run: told
# it knew nothing about the Corrector, a 1.7b invented that the Corrector is "a
# device used to alter records"; told it had already answered, it answered again
# in different words. Both branches now bypass generation entirely.
#
# This is plan.md §7 applied to conversation -- the LLM proposes, code disposes.
# It also makes the two branches free, which is the right cost for saying
# nothing.
DEFAULT_REFUSAL = "I know nothing of that."
DEFAULT_REPEAT = "I have told you what I know of that."

_LABELS = {
    "room": "About this place:",
    "monsters": "What is nearby:",
    "npc": "Others you know of:",
    "trade": "What you are carrying:",
    "item": "What is here to be carried:",
    "past": "You remember, from earlier delvers:",
}


# Authored canon is written without a subject -- "Keeps a plate warm for a
# shift that ended before the town emptied." -- and under "What is true of
# you:" a model supplies the subject itself. The third playtest's Widow:
# "You do keep a plate warm because I am a Widow..." (M14.1). In the first
# person the data is what the NPC would say: M13's lesson, that a small model
# reads data back more faithfully than it follows an instruction, for canon.
_MODALS = frozenset({"will", "would", "can", "could", "shall", "should", "must", "may", "might"})
_IRREGULAR = {"has": "have", "is": "am", "does": "do", "was": "was"}
# Sentence-openers that end in "s" and are not verbs.
_NOT_VERBS = frozenset({"this", "its", "his", "hers", "theirs", "yes", "less",
                        "always", "sometimes", "perhaps", "thus", "as"})


def _first_person(word: str) -> str | None:
    """A third-person singular verb in the first person, or None if `word`
    does not look like one."""
    w = word.lower()
    if w in _NOT_VERBS:
        return None
    if w in _IRREGULAR:
        return _IRREGULAR[w]
    if w in _MODALS:
        return w
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith(("ches", "shes", "sses", "xes", "zes")):
        return w[:-2]
    if len(w) > 2 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return None


def as_speaker(text: str) -> str:
    """Subjectless authored canon in the first person; anything else unchanged.

    The verb after "and" shares the missing subject ("...and does not want to
    go"), so it turns too. Pronouns do not: "what each delver owes her" stays,
    which reads as a slip rather than as someone else speaking.
    """
    first, _, rest = text.strip().partition(" ")
    verb = _first_person(first) if first[:1].isupper() else None
    if verb is None:
        return text
    rest = re.sub(r"\band (\w+)", lambda m: "and " + (_first_person(m.group(1)) or m.group(1)),
                  rest)
    return f"I {verb} {rest}".rstrip()


@dataclass(frozen=True)
class Fact:
    """One retrieved thing, with a stable identity.

    `key` is what lets the conversation session dedupe on *which fact* rather
    than on phrasing -- the difference between "I've already told you that" and
    hoping a 1.7b notices it is repeating itself.
    """

    key: str
    text: str


@dataclass
class Brief:
    """Everything one dialogue turn is allowed to say, and what to do with it."""

    route: str
    canon: list[str] = field(default_factory=list)
    told: list[str] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    label: str = ""
    instruction: str = _INSTRUCTIONS["none"]
    repeated: bool = False
    used_model: bool = False
    # Fact keys the session marks as said, once the reply actually happens.
    keys: list[str] = field(default_factory=list)
    # When set, this is what the NPC says -- verbatim, with **no model call**.
    # See `DEFAULT_REFUSAL`.
    spoken: str = ""


class Router:
    """Classifies a topic and resolves it. Owns no state of its own."""

    def __init__(self, theme, store, settings, client=None):
        self._theme = theme
        self._store = store
        self._settings = settings
        self._client = client

        # Roster names are routable without a code change, which is the same
        # rule the rest of the engine follows: structure is code's, names are
        # the theme's.
        names: set[str] = {"anyone", "someone", "else", "other", "others"}
        for npc in theme.npcs:
            names |= {w for w in npc.name.lower().split() if w not in {"the", "a", "an"}}
        self._keywords = {**_KEYWORDS, "npc": names}

    # -- classification ----------------------------------------------------

    def _self_words(self, actor) -> set[str]:
        """Distinctive nouns from this NPC's own canon.

        The Archivist's canon is all about a ledger, so "ask about the ledger"
        is a question about itself -- but no keyword list written in advance
        knows that, and the stage-2 classifier sent it somewhere else. Reading
        the words out of canon keeps it working for an NPC nobody has written
        yet, which is the same rule the roster names follow.

        Only words no other route claims, so canon mentioning "floor" cannot
        quietly hijack every question about the room.
        """
        taken: set[str] = set()
        for route, words in self._keywords.items():
            if route != "self":
                taken |= words
        out: set[str] = set()
        for row in self._safe_canon(actor.id):
            for w in row["text"].lower().replace(".", " ").replace(",", " ").split():
                if len(w) >= 5 and w not in taken:
                    out.add(w)
        return out

    def _safe_canon(self, anchor: str):
        try:
            return self._store.canon(anchor, provenance=("authored", "derived"))
        except Exception:
            return []

    def classify(self, topic: str, actor=None) -> tuple[str, bool]:
        """(route, whether a model call was spent). Never raises.

        An empty topic is not a miss. `talk to archivist` names nothing, and an
        NPC greeted with nothing to go on should volunteer the most important
        thing it knows -- that is the `past` route by the same reasoning
        `DEATH_QUERY` was chosen for. Never spend a tier-1 call deciding that
        nothing means something.
        """
        topic = topic.strip().lower()
        if not topic:
            return "past", False

        words = set(topic.split())
        if actor is not None and self._self_words(actor) & words:
            return "self", False
        # `trade` before `self`: "what are you carrying" and "who are you" both
        # contain "you", and only one of them is a question about identity.
        for route in ("trade", "self", "npc", "monsters", "item", "room", "past"):
            if self._keywords.get(route, set()) & words:
                return route, False

        return self._infer(topic), True

    def _infer(self, topic: str) -> str:
        """Stage 2, only on a miss. ~32 tokens, same shape as `parser.infer`.

        **A miss falls through to `past`, not to `unknown`.** The vector index
        is the one resolver that can answer an arbitrary topic -- that is the
        job systems.md §6 keeps it for -- so a topic no keyword and no
        classifier could place is exactly what it is for. `unknown` stays in the
        enum for a model that positively decides the question is about nothing,
        and either way an empty resolution still ends at "I don't know": `past`
        that finds nothing returns `[]` like every other route.
        """
        if self._client is None:
            return "past"
        try:
            data = self._client.structured(
                [
                    {"role": "system", "content":
                        "Say which one thing the question is about. One word."},
                    {"role": "user", "content":
                        f"Question: {topic}\n\nChoices: {', '.join(ROUTES)}."},
                ],
                ROUTE_SCHEMA,
                self._settings.intent,
                kind="tier1",
            )
        except Exception:
            return "past"
        route = str((data or {}).get("route", "")).strip().lower()
        return route if route in ROUTES else "past"

    # -- resolvers ---------------------------------------------------------
    #
    # Each is total, cheap, and -- except `past` -- makes no model call. That is
    # the whole efficiency claim, and `test_no_resolver_but_past_touches_the_model`
    # is what keeps it true.

    def _self(self, state, actor, topic) -> list[Fact]:
        """What this NPC is -- and how much of it they will tell *you*.

        A gift buys access to a route, not an item. Authored canon is the
        official line and anyone gets it; `derived` canon is the history the
        canonist worked out about them, and that takes being on good terms.

        Deliberately the only thing disposition gates upward, so that M4's gate
        (the Archivist remembers your last run) and M7's (a backstory that is
        not a death) both still hold at neutral.
        """
        kinds = ("authored", "derived") if is_warm(self._store, actor.id) else ("authored",)
        rows = self._store.canon(actor.id, provenance=kinds, limit=CANON_LIMIT)
        # Authored only: derived canon is the canonist's own sentences, and
        # "Entries for six delvers..." is not a verb waiting for a subject.
        return [Fact(f"canon:{r['id']}",
                     as_speaker(r["text"]) if r["provenance"] == "authored" else r["text"])
                for r in rows]

    def _trade(self, state, actor, topic) -> list[Fact]:
        """What they carry, and whether they are in a mood to part with it.

        M8 left this route out on purpose -- "adding a route that returns
        nothing would be a stub pretending to be a feature". It has somewhere to
        resolve into now.
        """
        if is_wary(self._store, actor.id):
            return []
        return [
            Fact(f"held:{r.get('id')}", f"You are carrying {r.get('name')}.")
            for r in holdings(self._store, actor.id)
        ][:WORLD_LIMIT]

    def _room(self, state, actor, topic) -> list[Fact]:
        room = state.room
        out = [Fact(f"room:{room.id}:name", f"This room is {room.name}.")]
        if node := self._store.node(f"room:{room.id}"):
            concept = (json.loads(node["data"] or "{}").get("concept") or "").strip()
            if concept:
                out.append(Fact(f"room:{room.id}:concept", concept))
        # M11: what the player has turned up here. Discoveries are canon on this
        # node, and until now the route that answers "what is this place" read
        # only the director's concept -- so the NPC standing beside something
        # you had just found had never heard of it.
        try:
            for r in self._store.canon(f"room:{room.id}", provenance="derived", limit=2):
                out.append(Fact(f"canon:{r['id']}", r["text"]))
        except Exception:
            pass
        if room.exits:
            ways = ", ".join(d.value for d in room.exits)
            out.append(Fact(f"room:{room.id}:exits", f"Ways out: {ways}."))
        return out[:WORLD_LIMIT]

    def _monsters(self, state, actor, topic) -> list[Fact]:
        """Here, then one room out.

        Adjacency comes from the live floor rather than from `Store.neighbors`:
        the graph records rooms and walls, not who is standing in them, so
        asking it would be a query that cannot answer the question.
        """
        out = []
        for a in state.room.actors:
            if a.hostile and a.hp > 0:
                out.append(Fact(f"monster:{a.id}", f"{a.name} is in this room."))
        for direction, dest in state.room.exits.items():
            room = state.floor.rooms.get(dest)
            if room is None:
                continue
            for a in room.actors:
                if a.hostile and a.hp > 0:
                    out.append(Fact(
                        f"monster:{a.id}", f"{a.name} is {direction.value} of here."
                    ))
        return out[:WORLD_LIMIT]

    def _npc(self, state, actor, topic) -> list[Fact]:
        """Other NPCs this run has met, and what they are.

        The first engine code to read another entity's canon, and the reason M7
        added a second NPC: the Corrector's "Does not trust the Archivist's
        count" is the material this route exists to surface.
        """
        out = []
        for npc in self._theme.npcs:
            if npc.anchor == actor.id or npc.anchor not in state.met_npcs:
                continue
            out.append(Fact(f"npc:{npc.anchor}", f"{npc.name} {npc.role}."))
            for r in self._store.canon(npc.anchor, provenance="authored", limit=1):
                out.append(Fact(f"canon:{r['id']}", f"{npc.name}: {r['text']}"))
        return out[:WORLD_LIMIT]

    def _item(self, state, actor, topic) -> list[Fact]:
        out = [
            Fact(f"item:{i.id}", f"{i.name} is in this room.")
            for i in state.room.items
        ]
        out += [
            Fact(f"item:{i.id}", f"The delver is carrying {i.name}.")
            for i in state.player.inventory
        ]
        return out[:WORLD_LIMIT]

    def _past(self, state, actor, topic) -> list[Fact]:
        """The one route that embeds anything.

        Prior runs only: without `exclude_run` an NPC "remembers" something from
        four turns ago as though it were a past life.
        """
        if self._client is None:
            return []
        query = topic.strip() or DEATH_QUERY
        try:
            embedding = self._client.embed([query], self._settings.embed)[0]
        except Exception:
            embedding = None
        try:
            found = self._store.recall(
                embedding=embedding, about=actor.id,
                exclude_run=state.run_id, limit=RECALL_LIMIT,
            )
        except Exception:
            return []
        # A dialogue transcript records the *shape* of a question and not its
        # answer -- "a delver approached and asked about the arm" -- which is
        # true and information-free. M7 found the same thing poisoning derived
        # canon. Drop them whenever anything with content survives.
        content = [r for r in found if r.kind != "dialogue"]
        found = content or found

        # Deaths last: a small model attends hardest to the end of its prompt,
        # and a death is the most worth saying out loud.
        found = sorted(found, key=lambda r: r.kind == "death")
        return [Fact(f"memory:{r.id}", self._to_listener(r.text, actor.name)) for r in found]

    @staticmethod
    def _to_listener(text: str, name: str) -> str:
        """A memory as the NPC holding it would say it.

        Memories are written from outside -- "A delver approached the Assayer on
        floor 1" -- and handed to the Assayer, a 1.7b read that back verbatim:
        narration about the NPC, in the NPC's own mouth (M11.1). Addressed to
        the listener, the same fact is something the NPC can say. The bare name
        goes too, for memories that recorded it as a topic ("asked about
        assayer").
        """
        bare = re.sub(r"^(?:the|a|an)\s+", "", name.strip(), flags=re.I)
        for form in dict.fromkeys((name.strip(), bare)):
            if form:
                text = re.sub(rf"\b{re.escape(form)}\b", "you", text, flags=re.I)
        return text[:1].upper() + text[1:]

    # -- assembly ----------------------------------------------------------

    def _lines(self, actor) -> tuple[str, str]:
        npc = next((n for n in self._theme.npcs if n.anchor == actor.id), None)
        refusal = (getattr(npc, "refusal", "") or "").strip() or DEFAULT_REFUSAL
        repeat = (getattr(npc, "repeat", "") or "").strip() or DEFAULT_REPEAT
        return refusal, repeat

    def brief(self, state, actor, topic: str, *, surfaced: set[str] | None = None) -> Brief:
        """Classify, resolve, and decide what the model is told to do with it."""
        route, used_model = self.classify(topic, actor)
        refusal, repeat = self._lines(actor)

        # Standing context, on every turn regardless of route: what this NPC is,
        # and what it has been told. The route decides what is *added* to that.
        canon_facts = self._safe(self._self, state, actor, topic)
        told_rows = self._safe_told(actor.id)

        brief = Brief(
            route=route,
            canon=[f.text for f in canon_facts],
            told=[f"A delver told you: {r['text']}" for r in told_rows],
            used_model=used_model,
        )

        if route == "self":
            # The standing canon section already *is* the answer; adding a
            # second copy of it under a heading would just spend tokens.
            answer = canon_facts
        else:
            answer = self._safe(getattr(self, f"_{route}", _none), state, actor, topic)

        if not answer:
            # T2, arriving free. No threshold, no calibration, no guard against
            # an embedding outage reading as maximal relevance -- an empty
            # resolution is a truth about the data.
            #
            # Except for a greeting. "I don't know anything about that" is the
            # right answer to a question that resolved to nothing, and the wrong
            # answer to someone who just walked up and said hello -- there was
            # no "that". An NPC with nothing to report speaks from what it is.
            asked_something = bool(topic.strip())
            if canon_facts and not asked_something:
                brief.keys = [f.key for f in canon_facts]
                brief.instruction = _INSTRUCTIONS["self"]
            else:
                # Stripping the data was not enough -- the model invented from
                # the question itself. So this branch does not generate at all.
                brief.canon, brief.told = [], []
                brief.instruction = _INSTRUCTIONS["none"]
                brief.spoken = refusal
            return brief

        # Suppress only when *every* fact is one this NPC has already said. A
        # rephrased question that resolves the same way is not the player asking
        # twice, and an NPC that stonewalls it is worse than one that repeats.
        if surfaced and all(f.key in surfaced for f in answer):
            # Same reasoning as the refusal: told to say "I already told you"
            # while still holding the material, the model said the material
            # again in different words. Measured live.
            brief.canon, brief.told = [], []
            brief.repeated = True
            brief.instruction = _INSTRUCTIONS["repeat"]
            brief.spoken = repeat
            return brief

        brief.keys = [f.key for f in answer]
        if route != "self":
            brief.facts = answer
            brief.label = _LABELS.get(route, "")
        brief.instruction = _INSTRUCTIONS.get(route, _INSTRUCTIONS["facts"])
        return brief

    def _safe(self, fn, *a) -> list[Fact]:
        try:
            return fn(*a)
        except Exception:
            return []

    def _safe_told(self, anchor: str):
        try:
            return self._store.canon(anchor, provenance="told", limit=TOLD_LIMIT)
        except Exception:
            return []


def _none(*_a) -> list[Fact]:
    """Resolver for `unknown`: there is nothing to look up, and that is fine."""
    return []
