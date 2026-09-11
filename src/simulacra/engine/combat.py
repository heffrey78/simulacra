"""Deterministic resolution for known verbs. No LLM, no I/O, seedable.

Everything here is testable without a model running, which is the point: the
balance curve is code's responsibility and must stay verifiable in CI.

There is no character progression. The player's stats are fixed for a run, while
floors scale with depth, so descending is a decision to trade safety for
distance. "Endless" means the dungeon eventually wins -- the balance test asserts
that curve rather than assuming a winnable end state.

MILESTONE M3.
"""

from __future__ import annotations

import random

from ..world.model import Actor, Item, Player
from .events import Damage, Event, Roll

BARE_HANDS = 2
CRIT = 20
FUMBLE = 1

# Player statuses combat reads (M11). Before this the only status in the game was
# "braced", and nothing anywhere read it -- it counted down and displayed.
SHAKEN = "shaken"          # the judge's (you, hinder): your footing is gone
HIDDEN = "hidden"          # the `hide` verb: out of sight
GUARDED = "guarded"        # the judge's (you, protect): ready for the blow (M13)
SHAKEN_PENALTY = 2         # defense, while shaken
HIDDEN_BONUS = 2           # to hit, on the attack that ends hiding
GUARD_BONUS = 3            # defense, for the one round it is up
HIDE_DIFFICULTY = 12


def player_defense(player: Player) -> int:
    return (player.defense
            - (SHAKEN_PENALTY if SHAKEN in player.effects else 0)
            + (GUARD_BONUS if GUARDED in player.effects else 0))


def best_weapon(player: Player) -> Item | None:
    """Only carried items count -- we never scan the room."""
    weapons = [i for i in player.inventory if i.damage > 0]
    return max(weapons, key=lambda i: i.damage) if weapons else None


def attack_roll(attacker_bonus: int, target_defense: int, rng: random.Random) -> tuple[int, bool]:
    """d20 + bonus vs defense. Returns (total, hit).

    A natural 20 always hits and a natural 1 always misses, so no amount of stat
    scaling makes a fight fully deterministic in either direction.
    """
    roll = rng.randint(1, 20)
    total = roll + attacker_bonus
    if roll == CRIT:
        return total, True
    if roll == FUMBLE:
        return total, False
    return total, total >= target_defense


def _damage(base: int, rng: random.Random) -> int:
    return max(1, rng.randint(1, base + 2))


def player_attacks(player: Player, target: Actor, rng: random.Random) -> list[Event]:
    events: list[Event] = []
    weapon = best_weapon(player)
    base = weapon.damage if weapon else BARE_HANDS

    # Striking from hiding is the point of hiding -- and it ends it.
    ambush = player.effects.pop(HIDDEN, None) is not None
    bonus = player.attack + (HIDDEN_BONUS if ambush else 0)

    total, hit = attack_roll(bonus, target.defense, rng)
    detail = f"with {weapon.name}" if weapon else "bare-handed"
    events.append(Roll(
        label="attack", total=total, target=target.defense, success=hit,
        detail=detail + (" from hiding" if ambush else ""),
    ))
    if not hit:
        return events

    amount = _damage(base, rng)
    target.hp -= amount
    events.append(Damage(target=target.name, amount=amount, hp_left=max(0, target.hp)))
    return events


def actors_attack(actors: list[Actor], player: Player, rng: random.Random) -> list[Event]:
    """Runs after the player's action, and only for hostiles still standing."""
    events: list[Event] = []
    if HIDDEN in player.effects:
        return events
    defense = player_defense(player)
    for actor in actors:
        if not actor.hostile or actor.hp <= 0:
            continue
        total, hit = attack_roll(actor.attack, defense, rng)
        events.append(Roll(
            label=f"{actor.name} attacks", total=total,
            target=defense, success=hit,
        ))
        if not hit:
            continue
        amount = _damage(actor.attack, rng)
        player.hp -= amount
        events.append(Damage(target="you", amount=amount, hp_left=max(0, player.hp)))
        if player.hp <= 0:
            break
    return events


def clear_dead(actors: list[Actor]) -> list[Actor]:
    """Returns the actors removed, so the caller can name them in events."""
    dead = [a for a in actors if a.hp <= 0]
    actors[:] = [a for a in actors if a.hp > 0]
    return dead
