"""Two-stage input: deterministic verbs first, LLM only on a miss.

Stage 1 is a table of verbs and aliases. It costs nothing and handles the ~90%
of turns that are movement, looking, inventory and attacking.

Stage 2 -- only for input stage 1 rejects -- is a tiny structured call that maps
free text onto a known verb, or reports that it's genuinely novel and should go
to the judge instead. Budget: 48 tokens (config.Settings.intent).

MILESTONE M1 (stage 1) / M3 (stage 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..world.model import Direction

Verb = Literal["move", "look", "take", "use", "inventory", "attack", "talk",
               "tell", "give", "request", "follow", "search", "hide", "equip",
               "enter", "descend", "wait", "quit"]

# Intent schema for the stage-2 fallback. Deliberately tiny -- every optional
# field is tokens the model spends and we wait for.
INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "verb": {"type": "string", "enum": [*Verb.__args__, "improvise"]},
        "target": {"type": "string"},
    },
    "required": ["verb", "target"],
}


@dataclass(frozen=True)
class Intent:
    verb: Verb | Literal["improvise"]
    target: str = ""
    # Who is being spoken to, for `talk` and `tell`. Empty for every other verb.
    addressee: str = ""
    raw: str = ""
    # True when stage 2 ran, i.e. this turn cost an LLM round trip.
    inferred: bool = False


# Aliases -> canonical verb. Multi-word entries are matched first (see `parse`),
# so "pick up lamp" cannot be read as verb "pick", and "talk to smith" cannot be
# read as verb "talk" with target "to smith".
VERB_ALIASES: dict[str, Verb] = {
    "go": "move", "walk": "move", "head": "move", "move": "move",
    "look at": "look", "look": "look", "l": "look",
    "examine": "look", "x": "look", "inspect": "look",
    "pick up": "take", "take": "take", "get": "take", "grab": "take",
    # Healing items are unusable without these -- a table entry, not a feature.
    "use": "use", "drink": "use", "apply": "use", "quaff": "use",
    "inventory": "inventory", "inv": "inventory", "i": "inventory",
    "attack": "attack", "hit": "attack", "kill": "attack", "fight": "attack",
    "punch": "attack", "kick": "attack", "chop": "attack", "strike": "attack",
    "stab": "attack", "slash": "attack", "swing": "attack", "smack": "attack",
    "karate chop": "attack", "drop kick": "attack",
    "talk to": "talk", "talk": "talk", "speak": "talk", "ask": "talk",
    # `tell` is an assertion, not a question -- the one place the player puts a
    # claim *into* the world. Kept a deterministic verb rather than a tier-1
    # "was that a statement?" classifier, which is M8's route work.
    "tell": "tell", "say to": "tell", "inform": "tell",
    # Social verbs. `ask X for Y` becomes `request` in `parse` -- the word that
    # separates it from `ask X about Y` is the connective, not the verb.
    "give": "give", "offer": "give", "hand": "give", "show": "give",
    "follow": "follow",
    # M11: the verbs the 2026-09-10 playtest reached for. Each one the parser did
    # not know cost a tier-1 call and was routed by a model that lifts targets
    # from the room -- `search` became `look at <the room's own name>` ten times
    # out of ten in that session's world file.
    "search": "search", "rummage": "search", "loot": "search",
    "hide": "hide",
    "equip": "equip", "wield": "equip",
    "enter": "enter",
    "descend": "descend", "stairs": "descend",
    "wait": "wait", "z": "wait",
    "quit": "quit", "exit": "quit", "q": "quit",
}

# Longest-first, so multi-word aliases win.
_ALIASES_BY_LENGTH: list[tuple[str, str]] = sorted(
    VERB_ALIASES.items(), key=lambda kv: -len(kv[0].split())
)

_ARTICLES = frozenset({"the", "a", "an"})

# Verbs whose target is really two things: who is being spoken to, and what
# about. Splitting them is syntax, so it belongs here -- before M8 it happened
# in `_topic_of()` inside the talk handler, three ordered stopword passes deep,
# and every new social verb needed another pass.
_ADDRESSED = frozenset({"talk", "tell", "follow", "request"})

# Sit on opposite sides of the name: "to archivist" is pure address, while
# "about archivist" is a topic that happens to be the NPC.
_ADDRESS_WORDS = frozenset({"to", "with", "at"})
_TOPIC_WORDS = frozenset({"about", "for", "on", "regarding", "re"})


def split_gift(target: str) -> tuple[str, str]:
    """(addressee, item) for `give X to Y` -- the reverse of `talk`.

    The thing comes first and the person after the connective, which is the
    opposite order from every other addressed verb, so it gets its own split
    rather than a flag on the general one.

    With no connective, `give archivist the blade` reads as person-then-thing;
    a bare `give blade` is a thing with no person, and the engine falls back to
    whoever is present.
    """
    words = target.split()
    for i, w in enumerate(words):
        if w in _ADDRESS_WORDS:
            return " ".join(words[i + 1 :]), " ".join(words[:i])
    if len(words) >= 2:
        return words[0], " ".join(words[1:])
    return "", target


def first_connective(target: str) -> str:
    """The first topic word in a target, or "". Distinguishes ask-for from ask-about."""
    for w in target.split():
        if w in _TOPIC_WORDS:
            return w
    return ""


def split_address(verb: str, target: str) -> tuple[str, str]:
    """(addressee, topic) for a social verb; ("", target) for anything else.

    Connectives only. Deciding *which* NPC is a resolution problem that needs
    the room's occupants, which the parser has no business knowing -- so the
    engine still matches this phrase against who is actually present.

    `tell` has no connective to split on, so the addressee is the leading token.
    That is right for every name in the theme pack and wrong for a multi-word
    one; the engine's name-aware match is the backstop, so a miss degrades to
    "which of them?" rather than to the wrong NPC.
    """
    if verb not in _ADDRESSED or not target:
        return "", target

    words = target.split()
    while words and words[0] in _ADDRESS_WORDS:
        words.pop(0)
    if not words:
        return "", ""

    for i, w in enumerate(words):
        if w in _TOPIC_WORDS:
            return " ".join(words[:i]), " ".join(words[i + 1 :])

    # No connective. One word is a name; several are a name and a question --
    # `ask archivist what are you carrying` is ordinary English, and reading the
    # whole thing as an address made its topic empty, which routed it to the
    # memory index and answered with a death.
    if len(words) >= 2:
        return words[0], " ".join(words[1:])
    return " ".join(words), ""

# Verbs that never take a target. Trailing words after these are ignored rather
# than treated as a parse failure.
_INTRANSITIVE = frozenset({"inventory", "wait", "quit", "descend", "hide"})

# "look"/"examine" filler that means no specific target -- "look around" must
# not be read as an attempt to examine something named "around".
_LOOK_FILLERS = frozenset({"around", "here", "about"})

# "follow me" addresses whoever is standing there, not someone named "me".
_SELF_WORDS = frozenset({"me", "us", "along"})


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def _strip_articles(text: str) -> str:
    tokens = [t for t in text.split() if t not in _ARTICLES]
    return " ".join(tokens)


def parse(text: str) -> Intent | None:
    """Stage 1. Returns None if no verb matched -- caller escalates to stage 2.

    Never raises. `None` is the escalation signal; making it an exception would
    force the M3 fallback into a try/except, which is the wrong shape.
    """
    raw = _normalise(text)
    if not raw:
        return None

    # Bare directions first. "n" and "north" are the most common inputs in the
    # game; requiring "go north" would be a usability failure.
    if (direction := Direction.parse(raw)) is not None:
        return Intent(verb="move", target=direction.value, raw=raw)

    for alias, verb in _ALIASES_BY_LENGTH:
        if raw == alias:
            rest = ""
        elif raw.startswith(alias + " "):
            rest = raw[len(alias) + 1 :]
        else:
            continue

        target = _strip_articles(rest)

        if verb in _INTRANSITIVE:
            return Intent(verb=verb, target="", raw=raw)

        if verb == "move":
            if not target:
                # "go" on its own -- a known verb missing an argument. The engine
                # asks "Go where?"; this is not a parse failure.
                return Intent(verb="move", target="", raw=raw)
            direction = Direction.parse(target)
            if direction is None:
                # "go to the well" is improvisation, not movement. Escalate.
                return None
            return Intent(verb="move", target=direction.value, raw=raw)

        if verb == "look" and target in _LOOK_FILLERS:
            target = ""

        # `ask archivist for the blade` and `ask archivist about the blade`
        # share a verb and mean different things. The connective is what tells
        # them apart, which is why the addressee slot had to exist first.
        if verb == "talk" and first_connective(target) == "for":
            verb = "request"

        # "follow me" addresses whoever is present, not someone called "me".
        if verb == "follow":
            target = " ".join(w for w in target.split() if w not in _SELF_WORDS)

        if verb == "give":
            addressee, target = split_gift(target)
        else:
            addressee, target = split_address(verb, target)
        return Intent(verb=verb, target=target, addressee=addressee, raw=raw)

    return None


def infer(text: str, room_summary: str, client, policy) -> Intent:
    """Stage 2. Structured call; falls back to `improvise` on any failure.

    Only ever reached on a stage-1 miss -- if this lands on the hot path, the
    "90% of turns cost zero LLM time" target in plan.md §2 is gone.

    Never raises. A failed classification degrades to improvisation, which the
    judge then rules on; it must not degrade to an error.
    """
    messages = [
        {"role": "system", "content":
            "Map the player's input to one verb. Use 'improvise' if it matches "
            "none of them. Answer only with the fields requested."},
        {"role": "user", "content":
            f"{room_summary}\nInput: {text}\n\nVerbs: {', '.join(Verb.__args__)}, improvise."},
    ]
    try:
        data = client.structured(messages, INTENT_SCHEMA, policy, kind="tier1")
        verb = str(data.get("verb", "")).strip().lower()
        target = _strip_articles(_normalise(str(data.get("target", ""))))
    except Exception:
        verb, target = "", ""

    if verb not in (*Verb.__args__, "improvise"):
        verb = "improvise"

    # A direction the model named as a target still has to be a real direction.
    if verb == "move" and Direction.parse(target) is None:
        verb, target = "improvise", ""

    # take/use act on the target unconditionally (first substring match in the
    # room/inventory) -- a target the model invented from room context rather
    # than the player's actual words ("karate chop offuct" -> target "ration",
    # lifted from the room census) would silently take or use the wrong thing.
    # Degrading to improvise is safe; the judge just rules on it instead.
    if verb in ("take", "use") and target and target not in _normalise(text):
        verb = "improvise"

    # The same lifting drove the playtest's search bug through `look`: "search
    # offcut", with the offcut already dead, became a look at "lair of the
    # forgotten" -- the room's name, taken from the summary. A look target that
    # shares no word with what the player typed is dropped, not trusted.
    if verb in ("look", "search") and target:
        if not set(target.split()) & set(_normalise(text).split()):
            target = ""

    target = target if verb != "improvise" else _normalise(text)
    if verb == "give":
        addressee, target = split_gift(target)
    else:
        addressee, target = split_address(verb, target)
    return Intent(
        verb=verb,  # type: ignore[arg-type]
        target=target,
        addressee=addressee,
        raw=_normalise(text),
        inferred=True,
    )
