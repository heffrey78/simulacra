"""Adjudication of improvised actions. The LLM proposes; code disposes.

The model never mutates game state. It returns a verdict against a *closed*
vocabulary of effects, and `apply_verdict` validates and applies it. A 1.7b
model asked to freeform game state will invent effects, invent numbers, and
quietly rewrite the difficulty curve; constraining it to this enum is what keeps
improvisation fun without making the game unbalanceable.

**Calibration.** M0 benchmarking ran "tip the brazier into the flooded room"
twice and both times got difficulty 20 -- the schema maximum -- with
`damage_self`, punishing exactly the play this exists to reward. A 1.7b model has
no intuition for an abstract 5-20 scale, so `_ANCHORS` gives it three worked
examples to interpolate against. The anchors live here rather than in the theme
pack because difficulty is engine balance; the theme owns voice.

**The reason is the model's; the effect is the engine's (M13).** The reason is
free text and the effect a closed enum, and nothing made them agree: `dodge` was
ruled *"prevents the enemy from attacking, reduces damage taken by 3"*, and the
enemy attacked on the same turn. So the reason is guarded like the narrator's
prose, its numbers are dropped (the engine clamps amounts after the model
answers, so a number it states may not be the one applied), and every effect the
engine applies is stated in the engine's own words after the roll.

MILESTONE M3.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Literal

from ..world.model import Player
from .events import Damage, Event, Improvised, Line, Roll, StatusChanged

EffectKind = Literal["damage_target", "damage_self", "heal_self", "status_target",
                     "status_self", "guard_self", "nothing"]

# Asked to choose one of six compound names (`damage_target`, `status_self`, ...)
# a 1.7b model picked one that contradicted its own stated reason in 3 of 5 live
# samples -- "the brazier scalds the ghoul" paired with `status_self`. Two small
# orthogonal choices are far easier for it than one six-way enum, and the pair
# still maps onto exactly the same closed vocabulary. The schema is the model's
# interface; `EffectKind` remains the engine's.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "plausible": {"type": "boolean"},
        "difficulty": {"type": "integer", "minimum": 5, "maximum": 20},
        "affects": {"type": "string", "enum": ["enemy", "you", "nobody"]},
        "kind": {"type": "string", "enum": ["damage", "hinder", "defend", "heal"]},
        "magnitude": {"type": "integer", "minimum": 0, "maximum": 8},
        "reason": {"type": "string"},
    },
    "required": ["plausible", "difficulty", "affects", "kind", "magnitude", "reason"],
}

# (affects, kind) -> EffectKind. Anything unmapped is `nothing`, which is why
# "heal the enemy" quietly does not exist.
#
# `defend` is M13's. The parser has sent `dodge`, `duck` and `roll` to the judge
# since M11.1, and the vocabulary had no defensive effect to give them -- so the
# model's reason promised one and the engine applied something else.
#
# Probed live before shipping. Offered as `protect`, it was chosen 0 times in 43
# rulings, and "protection" went under `heal`. Glossed as `defend`, it was
# chosen -- always with `affects: enemy`, because the enemy is who a defence is
# *against*. So `defend` is the player's guard whoever it names: a player's
# defence can shield no one else.
_EFFECT_MAP: dict[tuple[str, str], EffectKind] = {
    ("enemy", "damage"): "damage_target",
    ("enemy", "hinder"): "status_target",
    ("you", "damage"): "damage_self",
    ("you", "heal"): "heal_self",
    ("you", "hinder"): "status_self",
    ("you", "defend"): "guard_self",
    ("enemy", "defend"): "guard_self",
    ("nobody", "defend"): "guard_self",
}

# Hard ceilings applied after the model answers. Even inside the schema's range,
# a model that decides everything is magnitude 8 must not be able to trivialise
# the game -- these clamp it back into the curve code owns.
MAX_MAGNITUDE = {"damage_target": 6, "damage_self": 4, "heal_self": 3}

MIN_DIFFICULTY, MAX_DIFFICULTY = 5, 20

_ANCHORS = (
    " Calibrate difficulty against these: shoving an already unsteady opponent is 8; "
    "tipping a lit brazier into standing water to scald something is 12; "
    "talking a hostile creature into leaving is 18. "
    "Most clever, physically sensible actions belong between 8 and 14. "
    "Reserve 18-20 for genuine long shots."
)

_DEFAULT_SYSTEM = "You are a strict, fair referee. Be terse."

# Phrases that exist only in the judge's prompt. In a reason -- and not in what
# the player typed or the room they typed it in -- they are the model reading its
# instructions back. M11's pre-fix replay answered `hide` with "Tipping a lit
# brazier into standing water to scald something is 12": the prompt-transcription
# failure M2 built the narrator's guard for, arriving at the judge.
_PROMPT_ONLY = (
    "calibrate", "difficulty", "magnitude", "unsteady opponent", "brazier",
    "standing water", "into leaving", "genuine long shot", "room:", "action:",
    "carrying:", "`affects`",
)

_CLAUSE = re.compile(r"(?<=[,;.])\s+")


def _reads_its_prompt(reason: str, action: str, room: str) -> bool:
    r = reason.lower()
    context = f"{action} {room}".lower()
    return any(p in r and p not in context for p in _PROMPT_ONLY)


def _without_numbers(reason: str) -> str:
    """Drop the clauses that state a number, when any clause survives.

    "Dodging prevents the enemy from attacking, reduces damage taken by 3" is
    two claims, and the second is a number the engine never agreed to. What is
    left still says what the player did.
    """
    clauses = _CLAUSE.split(reason.strip())
    kept = [c for c in clauses if not re.search(r"\d", c)]
    if not kept or len(kept) == len(clauses):
        return reason
    text = " ".join(kept).strip().rstrip(",;")
    return text if text.endswith((".", "!", "?")) else text + "."


# Returned whenever the model fails us. Implausible, so a broken call can never
# become a free win.
def _refused(reason: str = "Nothing comes of it.") -> Verdict:
    return Verdict(plausible=False, difficulty=MAX_DIFFICULTY, effect="nothing",
                   magnitude=0, reason=reason)


@dataclass(frozen=True)
class Verdict:
    plausible: bool
    difficulty: int
    effect: EffectKind
    magnitude: int
    reason: str


def _coerce(data: dict) -> Verdict:
    """Clamp a model response into something the game can survive."""
    if not isinstance(data, dict):
        return _refused()

    # Accept the two-field form the model is asked for, and an explicit
    # `effect` for callers (and tests) that speak the engine's vocabulary.
    if "effect" in data:
        effect = str(data.get("effect") or "nothing")
        if effect not in EffectKind.__args__:
            effect = "nothing"
    else:
        effect = _EFFECT_MAP.get(
            (str(data.get("affects") or "").lower(), str(data.get("kind") or "").lower()),
            "nothing",
        )

    try:
        difficulty = int(data.get("difficulty", MAX_DIFFICULTY))
        magnitude = int(data.get("magnitude", 0))
    except (TypeError, ValueError):
        return _refused()

    difficulty = max(MIN_DIFFICULTY, min(MAX_DIFFICULTY, difficulty))
    magnitude = max(0, min(magnitude, MAX_MAGNITUDE.get(effect, 2)))

    return Verdict(
        plausible=bool(data.get("plausible")),
        difficulty=difficulty,
        effect=effect,  # type: ignore[arg-type]
        magnitude=magnitude,
        reason=_without_numbers(str(data.get("reason") or "").strip()) or "Nothing comes of it.",
    )


def adjudicate(action: str, room_summary: str, theme, client, policy, *,
               carrying: tuple[str, ...] | list[str] = ()) -> Verdict:
    """One structured call. On any failure, returns an implausible verdict --
    a failed judge call must never become a free win.

    `carrying` is what the player has on them (M13). Without it the judge ruled
    `water` as "pouring water onto the floor to cool off" with none carried.
    """
    pack = ", ".join(carrying) if carrying else "nothing"
    messages = [
        {"role": "system", "content": (theme.judge_system or _DEFAULT_SYSTEM) + _ANCHORS},
        {"role": "user", "content":
            f"Room: {room_summary}\nCarrying: {pack}\nAction: {action}\n\n"
            "Is it physically plausible here? Anything that needs a thing the "
            "player neither carries nor can see here is not. Give a difficulty; "
            "who it affects (the enemy, you, or nobody); what kind of effect: "
            # Each kind glossed, and `defend` by the verbs the parser sends here
            # (M13, probed live: 1 of 6 test actions ruled right with bare
            # kind names, 4 glossed, 5 with the verbs). Bare, "trip" was ruled
            # damage and pouring water over your own head a heal.
            "damage (hurts), hinder (trips, weakens or slows), defend (a "
            "dodge, duck, block or brace that shields you from the next "
            "blow), or heal (restores health -- only with something that "
            "heals); a magnitude; and a one-sentence reason "
            "written for the player, with no numbers in it. `affects` must "
            "match who your reason says is affected."},
    ]
    try:
        data = client.structured(messages, VERDICT_SCHEMA, policy, kind="tier1")
    except Exception:
        return _refused()
    verdict = _coerce(data)
    if _reads_its_prompt(verdict.reason, action, room_summary):
        # Refused rather than retried: a model that recited its instructions has
        # not understood the action, so its other fields are no better than its
        # reason. Same rule as any other failed call -- never a free win.
        return _refused()
    return verdict


def _named(name: str) -> str:
    return name[:1].upper() + name[1:]


def apply_verdict(verdict: Verdict, state, rng: random.Random) -> list[Event]:
    """Roll against the difficulty, clamp, apply, return Events."""
    from .combat import GUARDED, SHAKEN, attack_roll  # local: combat imports nothing from here

    action_text = getattr(state, "_last_action", "")
    if not verdict.plausible:
        return [Improvised(action=action_text, plausible=False, reason=verdict.reason)]

    player: Player = state.player
    room = state.room
    hostiles = [a for a in room.actors if a.hostile and a.hp > 0]

    # M11. A verdict aimed at an enemy in a room with none used to roll, report
    # a hit, and then silently apply nothing -- the playtest got "jumping over
    # the edge causes the enemy to stumble" in an empty room. No target means
    # no roll, and the model's reason, which invented the target, is not shown.
    if verdict.effect in ("damage_target", "status_target") and not hostiles:
        return [Improvised(action=action_text, plausible=False,
                           reason="There is nothing here for that to act on.")]

    # M13, measured: told the player carried nothing, the judge still ruled
    # "pour water over my head" a heal five times in five -- "heals player due
    # to natural moisture". A heal is the one effect that is pure gain with no
    # enemy involved, so like a target it needs a source: something carried or
    # here that heals. The prompt's rule said so; code has to.
    if verdict.effect == "heal_self" and not any(
        i.heal > 0 for i in (*player.inventory, *getattr(room, "items", ()))
    ):
        return [Improvised(action=action_text, plausible=False,
                           reason="You have nothing here to heal with.")]

    events: list[Event] = [
        Improvised(action=action_text, plausible=True, reason=verdict.reason)
    ]

    total, success = attack_roll(player.attack, verdict.difficulty, rng)
    events.append(Roll(label="improvise", total=total,
                       target=verdict.difficulty, success=success))
    if not success:
        return events

    amount = verdict.magnitude

    # Every branch says, in the engine's words, what was actually applied. The
    # reason above is the model's account and may promise more (M13).
    match verdict.effect:
        case "damage_target" if hostiles:
            target = hostiles[0]
            target.hp -= amount
            events.append(Damage(target=target.name, amount=amount,
                                 hp_left=max(0, target.hp)))
        case "damage_self":
            player.hp -= amount
            events.append(Damage(target="you", amount=amount, hp_left=max(0, player.hp)))
        case "heal_self":
            player.hp = min(player.max_hp, player.hp + amount)
            events.append(StatusChanged(hp=player.hp, max_hp=player.max_hp,
                                        depth=state.depth, effects=tuple(player.effects)))
        case "status_target" if hostiles:
            # Modelled as a defense penalty rather than a new subsystem. It was
            # applied silently until M13, so the player never knew it landed.
            hostiles[0].defense = max(5, hostiles[0].defense - max(1, amount))
            events.append(Line(f"{_named(hostiles[0].name)} is left open.", style="good"))
        case "status_self":
            # (you, hinder): the action cost you your footing. This was labelled
            # "braced" -- a buff's name on a penalty -- and nothing read it. It
            # is now called what it is, and combat reads it (M11).
            player.effects[SHAKEN] = max(1, amount)
            events.append(StatusChanged(hp=player.hp, max_hp=player.max_hp,
                                        depth=state.depth, effects=tuple(player.effects)))
        case "guard_self":
            # One: the tick after this turn's monster round ends it, so it
            # covers exactly the attacks it was made against.
            player.effects[GUARDED] = 1
            events.append(Line("Your guard is up for this round.", style="good"))
            events.append(StatusChanged(hp=player.hp, max_hp=player.max_hp,
                                        depth=state.depth, effects=tuple(player.effects)))
        case _:
            events.append(Line("It works, and nothing more comes of it.", style="dim"))

    return events
