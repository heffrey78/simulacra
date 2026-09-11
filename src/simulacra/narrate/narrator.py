"""Tier-2: streamed prose. Everything the player reads comes through here.

Prompt discipline is the whole job. The budget is ~180 output tokens and a
prompt kept under roughly 600, because prompt eval is not free on CPU either.
That means: no conversation history, no world dump. Each call gets the theme
voice, the room's concept line, its census, and nothing else. Continuity comes
from the memory layer injecting two or three specific recollections, not from a
growing context window.

**The echo guard.** Benchmarking in M0 found `qwen3:1.7b` intermittently
returning the room census back verbatim instead of narrating it -- having
produced good prose from the same prompt moments earlier. An intermittent
instruction-following failure cannot be prompted away with confidence, so output
is checked and rejected. Two mitigations, in order:

1. The census is labelled (`ROOM:`, `EXITS:`) so it reads as *data* rather than
   as prose to continue -- and so an echo is trivially detectable.
2. Only the first ~48 characters are buffered before streaming is released. An
   echo starts wrong immediately, so a prefix check catches it for about three
   tokens of delay rather than the whole generation.

On rejection: one stricter retry, then the procedural fallback. The player never
sees the failure.

MILESTONE M2 (rooms) / M4 (NPC dialogue with recall).
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections import Counter
from collections.abc import Iterator

from ..world.model import Floor, Room

# Characters buffered before prose is released to the renderer. Large enough to
# catch an echo, small enough that the pause is imperceptible.
GUARD_PREFIX_CHARS = 48

# Bumped when the room prompt changes shape, so cached prose from the old shape
# is regenerated rather than replayed. 2 = M11, items and actors removed.
# 3 = M12, the theme's role words and one mood per room.
PROSE_VERSION = 3

# Labels that only ever appear in the prompt. Any of them in the output means
# the model is transcribing rather than writing.
_CENSUS_LABELS = (
    "room:", "role:", "exits:", "contains:", "present:", "concept:", "mood:",
    # Dialogue's own labels. These only ever appear in the prompt, so seeing one
    # in the output means the model is transcribing rather than speaking.
    # Measured live: an NPC replied "The delver said: archivist vault on floor
    # two is already open." -- an echo the census labels alone did not catch,
    # because "said" is not "says" and the prefix check needs a verbatim match.
    "the delver says", "the delver said", "what is true of you",
    "you remember, from earlier", "a delver once told you",
)

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", text.lower()).strip()


def looks_like_echo(text: str, census: str, *, verbatim: bool = True) -> bool:
    """True when the model transcribed its prompt instead of narrating.

    `verbatim=False` for dialogue. The substring test assumes the prompt is data
    the model should *transform* -- true of a room census, which is why it was
    written that way in M2. It is false of a route's facts, which an NPC is
    being asked to *relay*: "There is a smudged figure west of here" appearing
    in both prompt and reply is the system working. Measured live as an NPC
    answering a question about its own room with "They say nothing."

    The label test still applies either way: a prompt label in the output is
    transcription under any reading.
    """
    t = _norm(text)
    if not t:
        return True
    if any(label in t for label in _CENSUS_LABELS):
        return True
    if not verbatim:
        return False
    # A prefix that appears verbatim in the census is a copy, not a description.
    return len(t) >= 12 and t in _norm(census)


def looks_degenerate(text: str) -> bool:
    """True when the model has fallen into repetition rather than writing.

    The echo guard's sibling. It catches a different failure: not copying the
    prompt, but collapsing into a loop -- the Archivist's first live reply
    repeated "1234567890" against every motif. Cheap, and it covers a whole
    class of small-model degeneration.
    """
    words = _norm(text).split()
    if len(words) >= 8:
        most = Counter(words).most_common(1)[0][1]
        if most / len(words) > 0.3:
            return True
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return len(lines) >= 4 and len(set(lines)) * 2 <= len(lines)


def recites_itself(text: str, name: str) -> bool:
    """True when an NPC's reply is its own system prompt read back.

    The prompt opens "You are the Assayer, who...", and the 1.7b answered a
    greeting with "You are a rogue assayer, unbound by conventional rules" --
    twice, in the third playtest's first run. No prompt label appears in it, so
    `looks_like_echo` let it through. An NPC may tell the delver "you are late";
    it may not tell the delver that *they* are the NPC.
    """
    t = _norm(text)
    if not (t.startswith("you are ") or t.startswith("you're ")):
        return False
    own = {w for w in _norm(name).split() if w not in ("the", "a", "an")}
    opening = set(re.findall(r"[a-z']+", t)[:12])
    return bool(own & opening)


def _as_stream(text: str, size: int = 12) -> Iterator[str]:
    """Replay cached prose as deltas so the renderer path is identical whether
    the text was just generated or came from SQLite."""
    for i in range(0, len(text), size):
        yield text[i : i + size]


class Narrator:
    def __init__(self, client, theme, store, policy):
        self._client = client
        self._theme = theme
        self._store = store
        self._policy = policy
        self._lock = threading.Lock()  # guards cache writes across the prefetcher

    # -- prompts -----------------------------------------------------------

    def _system(self) -> str:
        parts = [self._theme.narrator_system]
        # No motif list (M12). The whole list went into every description's
        # system prompt, and a small model treats a list as a checklist: the
        # second playtest's transcript had `tallow` 24 times. The theme's motifs
        # still reach rooms, one at a time, as the MOOD fallback in `_census`.
        if note := self._theme.style_note(motifs=False):
            parts.append(note)
        return " ".join(p for p in parts if p)

    def _census(self, floor: Floor, room: Room) -> str:
        # The theme's description of the role, never the engine's role word
        # (M12): told "shrine", the model wrote altars into a silver mine.
        lines = [f"ROOM: {room.name}", f"ROLE: {self._theme.role(room.kind)}"]
        if room.concept:
            lines.append(f"CONCEPT: {room.concept}")
        # One mood, rotated -- never the whole list (M12). Every floor motif on
        # every room made each floor's rooms the same room: run 2 of the second
        # playtest put the torch, the crumbling wall and the hollow altar in
        # every room of its first floor.
        if mood := self._mood(floor, room):
            lines.append(f"MOOD: {mood}")
        # No items, no actors (M11). Prose is cached, and since M6 the cache
        # outlives the run -- so anything the narrator is shown here is in the
        # description forever. The playtest took a jar of water and the room
        # still said "the jar of clean water sits beside the door, untouched".
        # `Engine._contents` lists what is actually here, every visit, which is
        # what M2 always meant the narrator's version to be backed by.
        lines.append(f"EXITS: {', '.join(d.value for d in room.exits)}")
        return "\n".join(lines)

    def _messages(self, census: str, *, strict: bool = False) -> list[dict]:
        instruction = (
            "Describe this room to the player. Do not repeat the labels or list "
            "the exits back."
        )
        if strict:
            instruction = (
                "Write two original sentences of description for this room. "
                "Do NOT copy any line above. Do NOT mention exits. Start with a "
                "concrete physical detail."
            )
        return [
            {"role": "system", "content": self._system()},
            {"role": "user", "content": f"{census}\n\n{instruction}"},
        ]

    def _mood(self, floor: Floor, room: Room) -> str:
        """This room's one mood: the floor's, rotated by room, else the theme's.

        The floor's motifs pass the director's mood rule on the way *in* since
        M12 -- but worlds made before M12 stored them as objects in particular
        rooms ("A hollow statue in the lair"), and those come back from the
        graph unfiltered. Same rule on the way out, so an old world stops
        putting the lair's statue in another room too.
        """
        from ..world.director import _mood as as_mood

        pool = [m for m in (as_mood(x) for x in floor.motifs) if m] or list(self._theme.motifs)
        if not pool:
            return ""
        order = sorted(floor.rooms)
        i = order.index(room.id) if room.id in order else 0
        return pool[i % len(pool)]

    def _key(self, floor: Floor, room: Room) -> str:
        # Concept is part of the key: if the director renames a floor, stale
        # prose describing the old concept must not survive.
        # PROSE_VERSION changes whenever the prompt's shape does. Worlds made
        # before M11 hold prose written from a census that listed items; without
        # the salt those keys would still match and the stale jar would stay.
        digest = hashlib.sha1(
            f"{PROSE_VERSION}|{self._theme.name}|{room.concept}|{floor.theme_name}".encode()
        ).hexdigest()[:8]
        return f"prose:{room.id}:{digest}"

    # -- generation --------------------------------------------------------

    def _generate(self, census: str, *, strict: bool, cancel=None) -> str | None:
        """Blocking generation with the echo guard. Returns None if rejected.

        Streams internally even though the caller wants a whole string: it lets
        the prefetcher abandon a request mid-flight (see prefetch.py) instead of
        holding the client lock for a full generation.
        """
        parts: list[str] = []
        gen = self._client.stream(
            self._messages(census, strict=strict), self._policy, kind="tier2"
        )
        try:
            for piece in gen:
                if cancel is not None and cancel.is_set():
                    return None
                parts.append(piece)
        finally:
            gen.close()

        text = "".join(parts).strip()
        if looks_like_echo(text, census) or looks_degenerate(text):
            return None
        return text

    def prepare(self, floor: Floor, room: Room, cancel=None) -> str | None:
        """Generate and cache without displaying. Used by the prefetcher."""
        key = self._key(floor, room)
        if (cached := self._store.cached_prose(key)) is not None:
            return cached

        census = self._census(floor, room)
        for strict in (False, True):
            text = self._generate(census, strict=strict, cancel=cancel)
            if cancel is not None and cancel.is_set():
                return None
            if text:
                with self._lock:
                    self._store.cache_prose(key, text)
                return text
        return None

    def room(self, floor: Floor, room: Room) -> Iterator[str]:
        """Stream a room description, or replay it instantly from cache."""
        key = self._key(floor, room)
        if (cached := self._store.cached_prose(key)) is not None:
            yield from _as_stream(cached)
            return

        census = self._census(floor, room)
        buffer: list[str] = []
        released = False
        gen = self._client.stream(self._messages(census), self._policy, kind="tier2")

        try:
            for piece in gen:
                buffer.append(piece)
                if released:
                    yield piece
                    continue
                # Hold back only until there is enough to judge.
                head = "".join(buffer)
                if len(head) >= GUARD_PREFIX_CHARS:
                    if looks_like_echo(head, census):
                        gen.close()
                        buffer.clear()
                        break
                    released = True
                    yield head
        finally:
            gen.close()

        text = "".join(buffer).strip()
        if released and text and not looks_like_echo(text, census):
            with self._lock:
                self._store.cache_prose(key, text)
            return

        if not released:
            # The prefix failed, or the whole response was too short to judge and
            # turned out to be an echo. Retry once, strictly, then give up.
            if retry := self._generate(census, strict=True):
                with self._lock:
                    self._store.cache_prose(key, retry)
                yield from _as_stream(retry)
            else:
                yield room.concept or f"{room.name}."

    def _detail_key(self, kind: str, name: str) -> str:
        # Keyed on kind+name+theme only, not the room -- the same offcut looks
        # the same wherever it's fought, and keying per-room would fragment the
        # cache for no benefit (unlike room prose, which is genuinely per-room).
        digest = hashlib.sha1(f"{self._theme.name}|{kind}|{name}".encode()).hexdigest()[:8]
        return f"detail:{kind}:{name}:{digest}"

    def _detail_messages(self, kind: str, name: str, room_concept: str, *, strict: bool = False) -> list[dict]:
        subject = f"{kind.upper()}: {name}"
        if room_concept:
            subject += f"\nROOM: {room_concept}"
        instruction = (
            "The player is looking closely at this. Describe it in one or two "
            "sentences. Do not repeat the labels."
        )
        if strict:
            instruction = (
                "Write one or two original sentences describing this closely. "
                "Do NOT copy any line above. Start with a concrete physical "
                "detail."
            )
        return [
            {"role": "system", "content": self._system()},
            {"role": "user", "content": f"{subject}\n\n{instruction}"},
        ]

    def detail(self, kind: str, name: str, room_concept: str = "") -> Iterator[str]:
        """Stream a close description of one item or actor the player looked
        at, or replay it instantly from cache.

        Same echo-guard discipline and cache-then-stream shape as `room()`.
        Looking at the same offcut twice mid-fight must be instant -- a second
        tier-2 call is exactly the wrong place to spend latency mid-combat.
        """
        key = self._detail_key(kind, name)
        if (cached := self._store.cached_prose(key)) is not None:
            yield from _as_stream(cached)
            return

        census = f"{kind.upper()}: {name}"
        buffer: list[str] = []
        released = False
        gen = self._client.stream(
            self._detail_messages(kind, name, room_concept), self._policy, kind="tier2"
        )
        try:
            for piece in gen:
                buffer.append(piece)
                if released:
                    yield piece
                    continue
                head = "".join(buffer)
                if len(head) >= GUARD_PREFIX_CHARS:
                    if looks_like_echo(head, census) or looks_degenerate(head):
                        gen.close()
                        buffer.clear()
                        break
                    released = True
                    yield head
        finally:
            gen.close()

        text = "".join(buffer).strip()
        if released and text and not looks_like_echo(text, census) and not looks_degenerate(text):
            with self._lock:
                self._store.cache_prose(key, text)
            return

        if not released:
            retry_gen = self._client.stream(
                self._detail_messages(kind, name, room_concept, strict=True),
                self._policy, kind="tier2",
            )
            try:
                parts = list(retry_gen)
            finally:
                retry_gen.close()
            retry = "".join(parts).strip()
            if retry and not looks_like_echo(retry, census) and not looks_degenerate(retry):
                with self._lock:
                    self._store.cache_prose(key, retry)
                yield from _as_stream(retry)
            else:
                yield f"Nothing more to notice about {name}."

    def npc(self, npc, player_line: str, brief) -> Iterator[str]:
        """Stream NPC speech from a route's `Brief`.

        The brief carries what was retrieved *and* what to do with it, because
        those two decisions belong together -- M4's unconditional "say what
        happened to the delver you remember" was correct for the only route that
        existed, and became a bug the moment a second kind of question could be
        asked. See `engine/routes.py`.

        Order follows what M4 measured: a small model attends hardest to the end
        of its prompt. Standing context first -- what this NPC is, then what it
        was told -- then the route's own facts, then the instruction, then the
        player's line last so it is the thing being answered.

        Never cached: a conversation that replays word for word is worse than
        one that costs six seconds.
        """
        system = (
            # "who {role}": roles are verb phrases in both theme packs, and
            # "You are the Assayer. weighs what you bring up..." is not a
            # sentence -- the model echoed it back as dialogue (M11.1).
            f"You are {npc.name}, who {npc.role}. {npc.voice} "
            "Speak only as this character, in one or two sentences. "
            "Do not narrate, and do not repeat these instructions."
        )
        if note := self._theme.style_note(motifs=False):
            system += " " + note

        parts: list[str] = []
        if brief.canon:
            parts.append("What is true of you:")
            parts.extend(f"- {c}" for c in brief.canon)
        if brief.told:
            # Attribution sits in the fact rather than in a rider. M7 measured
            # the rider ("say who told you") being ignored most of the time;
            # data the model reads back is obeyed where an instruction is not.
            parts.append("Things delvers have claimed to you, which may be false:")
            parts.extend(f"- {t}" for t in brief.told)
        if brief.facts:
            if brief.label:
                parts.append(brief.label)
            parts.extend(f"- {f.text}" for f in brief.facts)

        # Every branch carries one. A prompt of pure data makes a 1.7b
        # transcribe the last thing it was handed -- measured live in M7.
        parts.append(brief.instruction)
        parts.append(f"The delver says: {player_line or 'nothing; they simply approach.'}")
        user = "\n".join(parts)

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        buffer: list[str] = []
        released = False
        gen = self._client.stream(messages, self._policy, kind="tier2")
        try:
            for piece in gen:
                buffer.append(piece)
                if released:
                    yield piece
                    continue
                head = "".join(buffer)
                if len(head) >= GUARD_PREFIX_CHARS:
                    # Dialogue is not immune to the failures room prose had.
                    if (looks_like_echo(head, user, verbatim=False) or looks_degenerate(head)
                            or recites_itself(head, npc.name)):
                        gen.close()
                        buffer.clear()
                        break
                    released = True
                    yield head
        finally:
            gen.close()

        if not released:
            text = "".join(buffer).strip()
            bad = (not text or looks_like_echo(text, user, verbatim=False)
                   or looks_degenerate(text) or recites_itself(text, npc.name))
            yield self.SILENT if bad else text

    NOTHING_FOUND = "Nothing comes of the search."
    # What an NPC's turn becomes when every guard rejected the reply. It is
    # narration, and the engine renders it as such (M14.1) -- it once printed
    # as the NPC's own line: "the Assayer: They say nothing."
    SILENT = "They say nothing."

    def discover(self, target: str, room) -> Iterator[str]:
        """Name what the player turned up. Streamed, guarded, never cached here.

        Not cached in `prose_cache` on purpose: the engine writes the result to
        the room's canon instead, which is the thing that persists across runs
        and can be read back as a fact rather than replayed as prose. Two copies
        of the same text in two stores is how they drift apart.

        Yields `NOTHING_FOUND` when the guards reject the generation, and the
        caller must then write nothing down -- a rejected generation becoming
        permanent canon is M7's expensive lesson.
        """
        census = f"ROOM: {room.name}\nCONCEPT: {room.concept}\nLOOKING AT: {target}"
        messages = [
            {"role": "system", "content": self._system()},
            {"role": "user", "content":
                f"{census}\n\nThe player looks closely at the {target}. Write "
                f"one or two sentences about the {target} itself -- not about "
                f"the room, and not about anything else in it. It is scenery, "
                f"not treasure: do not offer it to be taken. Do not repeat the "
                f"labels."},
        ]

        buffer: list[str] = []
        released = False
        gen = self._client.stream(messages, self._policy, kind="tier2")
        try:
            for piece in gen:
                buffer.append(piece)
                if released:
                    yield piece
                    continue
                head = "".join(buffer)
                if len(head) >= GUARD_PREFIX_CHARS:
                    if looks_like_echo(head, census) or looks_degenerate(head):
                        gen.close()
                        buffer.clear()
                        break
                    released = True
                    yield head
        finally:
            gen.close()

        if not released:
            text = "".join(buffer).strip()
            bad = not text or looks_like_echo(text, census) or looks_degenerate(text)
            yield self.NOTHING_FOUND if bad else text

    def epitaph(self, cause: str, depth: int, turns: int) -> str:
        """One blocking line on death. The player has stopped playing; a 3s wait
        is fine here, and it's the only place in the game where that's true.

        Non-fatal: a run ends without an epitaph rather than crashing on the way
        out.
        """
        messages = [
            {"role": "system", "content": self._system()},
            {"role": "user", "content":
                f"A delver died on floor {depth} after {turns} turns. "
                f"Cause: {cause}. Write ONE sentence marking the death. "
                "No preamble, no quotation marks."},
        ]
        try:
            text = self._client.complete(
                messages, self._policy.with_(num_predict=60), kind="epitaph"
            )
        except Exception:
            return ""
        return " ".join(text.split()).strip('"')
