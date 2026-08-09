"""Combat, and the balance curve.

The simulation at the bottom is the important test: it is what stops a later
tweak silently making the game unwinnable, and it needs no model.
"""

from __future__ import annotations

import random
from collections import Counter

import pytest

from simulacra.engine.combat import (
    BARE_HANDS,
    actors_attack,
    attack_roll,
    best_weapon,
    clear_dead,
    player_attacks,
)
from simulacra.engine.events import Damage, Roll
from simulacra.world.floorgen import generate_floor
from simulacra.world.model import Actor, Item, Player


def mob(hp=10, attack=2, defense=10, hostile=True, name="a duplicate"):
    return Actor(id="m", name=name, hp=hp, max_hp=hp, attack=attack,
                 defense=defense, hostile=hostile)


class Rigged(random.Random):
    """Feeds an exact sequence, so a test can pin a hit or a miss."""

    def __init__(self, rolls):
        super().__init__(0)
        self._rolls = list(rolls)

    def randint(self, a, b):
        return self._rolls.pop(0) if self._rolls else super().randint(a, b)


# -- rolls -----------------------------------------------------------------


def test_natural_twenty_always_hits():
    total, hit = attack_roll(0, 99, Rigged([20]))
    assert hit


def test_natural_one_always_misses():
    total, hit = attack_roll(99, 1, Rigged([1]))
    assert not hit


def test_roll_is_bonus_plus_d20_against_defense():
    total, hit = attack_roll(3, 15, Rigged([12]))
    assert total == 15 and hit
    total, hit = attack_roll(3, 15, Rigged([11]))
    assert total == 14 and not hit


# -- attacks ---------------------------------------------------------------


def test_player_hit_deals_damage_and_reports_it():
    target = mob(hp=10, defense=5)
    events = player_attacks(Player(), target, Rigged([20, 3]))
    assert isinstance(events[0], Roll) and events[0].success
    dmg = next(e for e in events if isinstance(e, Damage))
    assert target.hp == 10 - dmg.amount
    assert dmg.hp_left == target.hp


def test_player_miss_deals_nothing():
    target = mob(hp=10, defense=99)
    events = player_attacks(Player(), target, Rigged([1]))
    assert target.hp == 10
    assert not any(isinstance(e, Damage) for e in events)


def test_best_carried_weapon_is_used():
    player = Player()
    player.inventory = [Item(id="a", name="knife", damage=3),
                        Item(id="b", name="blade", damage=5),
                        Item(id="c", name="ration", heal=4)]
    assert best_weapon(player).name == "blade"


def test_bare_hands_when_carrying_nothing():
    assert best_weapon(Player()) is None
    events = player_attacks(Player(), mob(defense=5), Rigged([20, 2]))
    assert "bare-handed" in events[0].detail


def test_room_items_do_not_count_only_carried_ones():
    player = Player()  # empty inventory; a weapon lying in the room is irrelevant
    assert best_weapon(player) is None


def test_only_hostiles_attack():
    player = Player()
    friendly = mob(hostile=False)
    events = actors_attack([friendly], player, Rigged([20, 3]))
    assert events == [] and player.hp == player.max_hp


def test_dead_actors_do_not_attack():
    player = Player()
    corpse = mob(hp=0)
    assert actors_attack([corpse], player, Rigged([20, 3])) == []


def test_actors_stop_once_the_player_is_down():
    player = Player(hp=1)
    events = actors_attack([mob(defense=1), mob(defense=1)], player,
                           Rigged([20, 5, 20, 5]))
    assert player.hp <= 0
    assert sum(1 for e in events if isinstance(e, Damage)) == 1


def test_clear_dead_removes_and_returns_them():
    actors = [mob(hp=0, name="dead"), mob(hp=5, name="alive")]
    dead = clear_dead(actors)
    assert [a.name for a in dead] == ["dead"]
    assert [a.name for a in actors] == ["alive"]


# -- balance ---------------------------------------------------------------


def _use_healing(player: Player) -> bool:
    for item in player.inventory:
        if item.heal > 0:
            player.hp = min(player.max_hp, player.hp + item.heal)
            player.inventory.remove(item)
            return True
    return False


def _fight(player: Player, actors: list[Actor], rng: random.Random) -> bool:
    """Returns True if the player survives the room. Drinks when badly hurt."""
    for _ in range(80):
        alive = [a for a in actors if a.hp > 0]
        if not alive:
            return True
        if player.hp < player.max_hp * 0.35 and _use_healing(player):
            pass  # a turn spent healing
        else:
            player_attacks(player, alive[0], rng)
            clear_dead(actors)
        actors_attack([a for a in actors if a.hp > 0], player, rng)
        if player.hp <= 0:
            return False
    return player.hp > 0


MAX_DEPTH = 40


def simulate_run(seed: int, theme) -> int:
    """Play a whole run greedily and return the depth reached.

    A per-floor simulation would be dishonest: the player carries damage,
    inventory and progression between floors, and attrition across floors is
    what actually ends a run.
    """
    rng = random.Random(seed)
    player = Player()
    depth = 1
    while depth <= MAX_DEPTH:
        floor = generate_floor(depth, theme, random.Random(seed + depth))
        for room in floor.rooms.values():
            player.inventory.extend(room.items)
            if room.actors and not _fight(player, list(room.actors), rng):
                return depth
        depth += 1
        player.on_descend(depth)
    return depth


def depths(theme, trials: int = 120) -> list[int]:
    return sorted(simulate_run(s, theme) for s in range(trials))


@pytest.fixture(scope="module")
def reached(theme):
    return depths(theme)


def test_runs_get_somewhere(reached):
    """A run that dies on floor 1 or 2 makes the memory layer pointless."""
    median = reached[len(reached) // 2]
    assert median >= 3, f"median run reaches only floor {median}"


def test_deaths_are_spread_across_floors(reached):
    """No single floor may dominate. A hard tier threshold once put 61% of all
    deaths on exactly floor 4, which reads as a bug, not a difficulty curve."""
    counts = Counter(reached)
    worst = max(counts.values()) / len(reached)
    assert worst < 0.5, f"{worst:.0%} of runs end on one floor"
    assert len(counts) >= 5, "runs end on too few distinct floors"


def test_runs_do_end(reached):
    """'Endless' means the dungeon eventually wins, not that it can be beaten."""
    assert max(reached) < MAX_DEPTH, "some run never died"
    assert reached[int(len(reached) * 0.9)] < 25


def test_early_floors_are_rarely_fatal(reached):
    early = sum(1 for d in reached if d <= 2) / len(reached)
    assert early < 0.15, f"{early:.0%} of runs die by floor 2"


def test_progression_is_what_makes_depth_reachable(theme):
    """Without on_descend the run capped out around floor 4 -- monster HP scales
    with depth and player damage does not. This pins why the mechanic exists."""
    saved = Player.on_descend
    try:
        Player.on_descend = lambda self, depth: None
        without = depths(theme, trials=80)
    finally:
        Player.on_descend = saved

    with_prog = depths(theme, trials=80)
    assert with_prog[40] > without[40], "progression did not extend runs"
