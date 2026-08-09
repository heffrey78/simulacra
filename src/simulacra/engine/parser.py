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
               "descend", "wait", "quit"]

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
    raw: str = ""
    # True when stage 2 ran, i.e. this turn cost an LLM round trip.
    inferred: bool = False


# Aliases -> canonical verb. Multi-word entries are matched first (see `parse`),
# so "pick up lamp" cannot be read as verb "pick", and "talk to smith" cannot be
# read as verb "talk" with target "to smith".
VERB_ALIASES: dict[str, Verb] = {
    "go": "move", "walk": "move", "head": "move", "move": "move",
    "look": "look", "l": "look", "examine": "look", "x": "look", "inspect": "look",
    "pick up": "take", "take": "take", "get": "take", "grab": "take",
    # Healing items are unusable without these -- a table entry, not a feature.
    "use": "use", "drink": "use", "apply": "use", "quaff": "use",
    "inventory": "inventory", "inv": "inventory", "i": "inventory",
    "attack": "attack", "hit": "attack", "kill": "attack", "fight": "attack",
    "talk to": "talk", "talk": "talk", "speak": "talk", "ask": "talk",
    "descend": "descend", "stairs": "descend",
    "wait": "wait", "z": "wait",
    "quit": "quit", "exit": "quit", "q": "quit",
}

# Longest-first, so multi-word aliases win.
_ALIASES_BY_LENGTH: list[tuple[str, str]] = sorted(
    VERB_ALIASES.items(), key=lambda kv: -len(kv[0].split())
)

_ARTICLES = frozenset({"the", "a", "an"})

# Verbs that never take a target. Trailing words after these are ignored rather
# than treated as a parse failure -- "look around" should just look.
_INTRANSITIVE = frozenset({"look", "inventory", "wait", "quit", "descend"})


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

        return Intent(verb=verb, target=target, raw=raw)

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

    return Intent(
        verb=verb,  # type: ignore[arg-type]
        target=target if verb != "improvise" else _normalise(text),
        raw=_normalise(text),
        inferred=True,
    )
