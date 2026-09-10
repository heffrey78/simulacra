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

MILESTONE M3.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

from ..world.model import Player
from .events import Damage, Event, Improvised, Roll, StatusChanged

EffectKind = Literal["damage_target", "damage_self", "heal_self", "status_target", "status_self", "nothing"]

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
        "kind": {"type": "string", "enum": ["damage", "hinder", "heal"]},
        "magnitude": {"type": "integer", "minimum": 0, "maximum": 8},
        "reason": {"type": "string"},
    },
    "required": ["plausible", "difficulty", "affects", "kind", "magnitude", "reason"],
}

# (affects, kind) -> EffectKind. Anything unmapped is `nothing`, which is why
# "heal the enemy" quietly does not exist.
_EFFECT_MAP: dict[tuple[str, str], EffectKind] = {
    ("enemy", "damage"): "damage_target",
    ("enemy", "hinder"): "status_target",
    ("you", "damage"): "damage_self",
    ("you", "heal"): "heal_self",
    ("you", "hinder"): "status_self",
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
        reason=str(data.get("reason") or "").strip() or "Nothing comes of it.",
    )


def adjudicate(action: str, room_summary: str, theme, client, policy) -> Verdict:
    """One structured call. On any failure, returns an implausible verdict --
    a failed judge call must never become a free win."""
    messages = [
        {"role": "system", "content": (theme.judge_system or _DEFAULT_SYSTEM) + _ANCHORS},
        {"role": "user", "content":
            f"Room: {room_summary}\nAction: {action}\n\n"
            "Is it physically plausible here? Give a difficulty; who it affects "
            "(the enemy, you, or nobody); what kind of effect (damage, hinder, "
            "heal); a magnitude; and a one-sentence reason written for the "
            "player. `affects` must match who your reason says is affected."},
    ]
    try:
        data = client.structured(messages, VERDICT_SCHEMA, policy, kind="tier1")
    except Exception:
        return _refused()
    return _coerce(data)


def apply_verdict(verdict: Verdict, state, rng: random.Random) -> list[Event]:
    """Roll against the difficulty, clamp, apply, return Events."""
    from .combat import SHAKEN, attack_roll  # local: combat imports nothing from here

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

    events: list[Event] = [
        Improvised(action=action_text, plausible=True, reason=verdict.reason)
    ]

    total, success = attack_roll(player.attack, verdict.difficulty, rng)
    events.append(Roll(label="improvise", total=total,
                       target=verdict.difficulty, success=success))
    if not success:
        return events

    amount = verdict.magnitude

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
            # Modelled as a defense penalty rather than a new subsystem.
            hostiles[0].defense = max(5, hostiles[0].defense - max(1, amount))
        case "status_self":
            # (you, hinder): the action cost you your footing. This was labelled
            # "braced" -- a buff's name on a penalty -- and nothing read it. It
            # is now called what it is, and combat reads it (M11).
            player.effects[SHAKEN] = max(1, amount)
            events.append(StatusChanged(hp=player.hp, max_hp=player.max_hp,
                                        depth=state.depth, effects=tuple(player.effects)))
        case _:
            pass

    return events
