"""Floor generation must be correct without a model running.

These are the invariants that keep 'endless' from meaning 'eventually broken'.
"""

from __future__ import annotations

import math
import random
from collections import deque

import pytest

from simulacra.world.floorgen import MIN_TRAVERSAL, generate_floor
from simulacra.world.model import RoomKind
from simulacra.world.theme import Theme

# Rooms that hold the floor's actual content. All sit on the spine, so a loop
# that short-circuits the spine skips the entire point of the floor.
CONTENT_KINDS = {RoomKind.VAULT, RoomKind.LAIR, RoomKind.SHRINE}


@pytest.fixture(scope="module")
def theme() -> Theme:
    return Theme.load("simulacra")


def _reachable(floor) -> set[str]:
    seen, stack = set(), [floor.entrance_id]
    while stack:
        rid = stack.pop()
        if rid in seen:
            continue
        seen.add(rid)
        stack.extend(floor.rooms[rid].exits.values())
    return seen


@pytest.mark.parametrize("depth", range(1, 26))
def test_every_floor_is_fully_connected(theme, depth):
    floor = generate_floor(depth, theme)
    assert _reachable(floor) == set(floor.rooms)


@pytest.mark.parametrize("depth", range(1, 26))
def test_descent_is_always_reachable(theme, depth):
    floor = generate_floor(depth, theme)
    descents = [r for r in floor.rooms.values() if r.kind is RoomKind.DESCENT]
    assert len(descents) == 1
    assert descents[0].id in _reachable(floor)


@pytest.mark.parametrize("depth", range(1, 26))
def test_exits_are_symmetric(theme, depth):
    floor = generate_floor(depth, theme)
    for room in floor.rooms.values():
        for direction, dest in room.exits.items():
            assert floor.rooms[dest].exits.get(direction.opposite) == room.id


def test_generation_is_deterministic_under_seed(theme):
    a = generate_floor(7, theme, random.Random(42))
    b = generate_floor(7, theme, random.Random(42))
    assert [r.name for r in a.rooms.values()] == [r.name for r in b.rooms.values()]


def test_difficulty_rises_with_depth(theme):
    shallow = sum(m.hp for r in generate_floor(1, theme).rooms.values() for m in r.actors)
    deep = sum(m.hp for r in generate_floor(15, theme).rooms.values() for m in r.actors)
    assert deep > shallow


# -- pacing ----------------------------------------------------------------
#
# Loops are added after the spine, and originally nothing stopped one wiring the
# entrance straight to the stairs. That made the route ~3 steps at every depth
# (so larger floors were explored proportionally less) and let 24-43% of seeds
# skip the vault, lair and shrine entirely. These lock in the guard.


def _route(floor) -> list[str]:
    """Shortest entrance -> descent path as a list of room ids."""
    goal = next(r for r in floor.rooms.values() if r.kind is RoomKind.DESCENT).id
    prev: dict[str, str | None] = {floor.entrance_id: None}
    q = deque([floor.entrance_id])
    while q:
        rid = q.popleft()
        for dest in floor.rooms[rid].exits.values():
            if dest not in prev:
                prev[dest] = rid
                q.append(dest)
    path, cur = [], goal
    while cur is not None:
        path.append(cur)
        cur = prev[cur]
    return list(reversed(path))


@pytest.mark.parametrize("depth", range(1, 16))
def test_route_to_descent_respects_min_traversal(theme, depth):
    for seed in range(40):
        floor = generate_floor(depth, theme, random.Random(seed))
        size = len(floor.rooms)
        required = max(2, math.ceil((size - 1) * MIN_TRAVERSAL))
        assert len(_route(floor)) - 1 >= required, f"depth {depth} seed {seed} is a shortcut"


@pytest.mark.parametrize("depth", [1, 4, 8, 12])
def test_no_floor_lets_you_skip_all_its_content(theme, depth):
    for seed in range(60):
        floor = generate_floor(depth, theme, random.Random(seed))
        on_route = [floor.rooms[rid].kind for rid in _route(floor)]
        assert CONTENT_KINDS & set(on_route), f"depth {depth} seed {seed} skips every content room"


def test_traversal_grows_with_depth(theme):
    """The original bug was a flat ~3 steps regardless of floor size."""
    def avg(depth):
        return sum(len(_route(generate_floor(depth, theme, random.Random(s)))) - 1
                   for s in range(60)) / 60

    assert avg(12) > avg(6) > avg(1)


@pytest.mark.parametrize("depth", [4, 8, 12])
def test_loops_survive_the_guard(theme, depth):
    """The guard must not quietly turn floors back into hallways."""
    extra = 0
    for seed in range(40):
        floor = generate_floor(depth, theme, random.Random(seed))
        edges = sum(len(r.exits) for r in floor.rooms.values()) // 2
        extra += edges - (len(floor.rooms) - 1)
    assert extra / 40 >= 1.0, "loops were rejected into nonexistence"
