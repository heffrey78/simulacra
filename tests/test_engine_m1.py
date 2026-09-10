"""M1 engine loop. Runs with the Ollama daemon stopped -- that is the point.

Assertions are on event *types and order*, never on prose text: the placeholder
strings are replaced wholesale in M2 and tests that pin them would all break.
"""

from __future__ import annotations

from collections import deque

import pytest

from simulacra.config import Settings
from simulacra.engine.events import (
    FloorDescended,
    ItemTaken,
    Line,
    Notice,
    RoomEntered,
    RunEnded,
    StatusChanged,
)
from simulacra.engine.loop import Engine
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.world.model import RoomKind
from simulacra.world.theme import Theme

SEED = 42


class FakeRenderer:
    """Collects events without touching a terminal."""

    def __init__(self):
        self.events = []

    def handle(self, event):
        self.events.append(event)

    def types(self):
        return [type(e) for e in self.events]

    def of(self, cls):
        return [e for e in self.events if isinstance(e, cls)]


@pytest.fixture
def game(tmp_path):
    theme = Theme.load("simulacra")
    store = Store(tmp_path / "w.db")
    settings = Settings()
    state = new_run(store, theme, settings, seed=SEED)
    engine = Engine(state, store, settings, theme)
    yield engine, state, store
    store.close()


def drain(engine, text):
    r = FakeRenderer()
    for e in engine.turn(text):
        r.handle(e)
    return r


def route(floor, start, goal):
    """Shortest path as a list of direction strings."""
    prev = {start: None}
    q = deque([start])
    while q:
        rid = q.popleft()
        if rid == goal:
            break
        for direction, dest in floor.rooms[rid].exits.items():
            if dest not in prev:
                prev[dest] = (rid, direction)
                q.append(dest)
    steps, cur = [], goal
    while prev[cur] is not None:
        cur, direction = prev[cur]
        steps.append(direction.value)
    return list(reversed(steps))


# -- opening ---------------------------------------------------------------


def test_begin_emits_room_then_status_then_prose(game):
    engine, state, _ = game
    r = FakeRenderer()
    for e in engine.begin():
        r.handle(e)

    types = r.types()
    room_at = types.index(RoomEntered)
    # Cheap-and-certain before expensive: status precedes any description.
    assert types.index(StatusChanged) == room_at + 1
    assert r.of(RoomEntered)[0].first_visit is True
    assert r.of(RoomEntered)[0].room_id == state.floor.entrance_id


# -- movement --------------------------------------------------------------


def test_valid_move_enters_a_new_room(game):
    engine, state, _ = game
    list(engine.begin())
    direction = next(iter(state.room.exits))
    dest = state.room.exits[direction]

    r = drain(engine, direction.value)
    assert r.of(RoomEntered)[0].room_id == dest
    assert state.room_id == dest


def test_room_entered_reports_the_direction_travelled(game):
    """M5. `via` is the map's only source of layout -- a renderer builds its grid
    by offsetting from the previous room. If this drifts the map is wrong in a
    way nobody notices, because the prose still reads correctly.

    Note it is the *resolved* direction, not the typed token: "n" must arrive as
    "north" or the renderer has to re-implement the parser's aliases.
    """
    engine, state, _ = game
    list(engine.begin())
    direction = next(iter(state.room.exits))

    r = drain(engine, direction.value[0])   # typed as an alias: "n", "e", ...
    assert r.of(RoomEntered)[0].via == direction.value


def test_arrivals_that_are_not_moves_have_no_direction(game):
    """`begin`, `look` and `descend` are arrivals with no direction to report.
    None is the signal to place a room rather than offset from the last one."""
    engine, state, _ = game
    r = FakeRenderer()
    for e in engine.begin():
        r.handle(e)
    assert r.of(RoomEntered)[0].via is None

    assert drain(engine, "look").of(RoomEntered)[0].via is None

    descent = next(x for x in state.floor.rooms.values() if x.kind is RoomKind.DESCENT)
    state.room_id = descent.id
    entered = drain(engine, "descend").of(RoomEntered)
    assert entered and entered[0].via is None


def test_invalid_direction_is_refused_without_moving(game):
    engine, state, _ = game
    list(engine.begin())
    before = state.room_id
    blocked = [d for d in ("north", "south", "east", "west")
               if d not in {x.value for x in state.room.exits}]

    r = drain(engine, blocked[0])
    assert r.of(Notice)
    assert not r.of(RoomEntered)
    assert state.room_id == before


def test_rejected_input_still_costs_a_turn(game):
    engine, state, _ = game
    list(engine.begin())
    before = state.turns
    drain(engine, "xyzzy")          # unparsed
    drain(engine, "north north")    # parses to nothing useful
    assert state.turns == before + 2


def test_unparsed_input_gets_a_notice(game):
    engine, _, _ = game
    list(engine.begin())
    r = drain(engine, "flood the room with water")
    assert r.of(Notice)  # stage 2 arrives in M3


# -- items -----------------------------------------------------------------


def test_take_moves_an_item_by_substring(game):
    engine, state, _ = game
    list(engine.begin())
    room = next(r for r in state.floor.rooms.values() if r.items)
    state.room_id = room.id
    item = room.items[0]
    needle = item.name.split()[-1]

    r = drain(engine, f"take {needle}")
    assert r.of(ItemTaken)
    assert item not in room.items
    assert item in state.player.inventory


def test_take_absent_item_is_refused(game):
    engine, state, _ = game
    list(engine.begin())
    r = drain(engine, "take unicorn")
    assert r.of(Notice)
    assert state.player.inventory == []


def test_take_with_a_whitespace_only_target_does_not_grab_the_first_item(game):
    """A stage-2 misclassification can hand `_take` a near-empty target ("" or
    " "). An empty needle is a substring of every item name, so without a
    guard the first item in the room gets silently taken."""
    engine, state, _ = game
    list(engine.begin())
    room = next(r for r in state.floor.rooms.values() if r.items)
    state.room_id = room.id
    item = room.items[0]

    events = list(engine._take("   "))
    assert any(isinstance(e, Notice) for e in events)
    assert item in room.items
    assert state.player.inventory == []


def test_inventory_reports_empty_then_full(game):
    engine, state, _ = game
    list(engine.begin())
    assert "nothing" in drain(engine, "inventory").of(Line)[0].text.lower()

    room = next(r for r in state.floor.rooms.values() if r.items)
    state.room_id = room.id
    drain(engine, f"take {room.items[0].name.split()[-1]}")
    assert len(drain(engine, "i").of(Line)) >= 2


# -- descent ---------------------------------------------------------------


def test_descend_from_wrong_room_is_refused(game):
    engine, state, _ = game
    list(engine.begin())
    assert state.room.kind is not RoomKind.DESCENT
    r = drain(engine, "descend")
    assert r.of(Notice)
    assert state.depth == 1


def test_full_traversal_entrance_to_next_floor(game):
    """The M1 acceptance criterion: walk the floor and go down, no model."""
    engine, state, _ = game
    list(engine.begin())

    descent = next(r for r in state.floor.rooms.values() if r.kind is RoomKind.DESCENT)
    for step in route(state.floor, state.floor.entrance_id, descent.id):
        drain(engine, step)
    assert state.room_id == descent.id

    r = drain(engine, "descend")
    assert r.of(FloorDescended)
    assert state.depth == 2
    assert state.room_id == state.floor.entrance_id
    assert state.floor.depth == 2


def test_down_in_a_descent_room_descends(game):
    """The ambiguous-`down` resolution: parser says move(DOWN), engine decides."""
    engine, state, _ = game
    list(engine.begin())
    descent = next(r for r in state.floor.rooms.values() if r.kind is RoomKind.DESCENT)
    state.room_id = descent.id

    assert drain(engine, "down").of(FloorDescended)
    assert state.depth == 2


# -- lifecycle -------------------------------------------------------------


def test_quit_ends_the_run(game):
    engine, state, _ = game
    list(engine.begin())
    ended = drain(engine, "quit").of(RunEnded)
    assert ended and ended[0].cause == "quit"


def test_same_seed_produces_the_same_floor(tmp_path):
    theme = Theme.load("simulacra")
    names = []
    for i in (0, 1):
        store = Store(tmp_path / f"{i}.db")
        state = new_run(store, theme, Settings(), seed=SEED)
        names.append([r.name for r in state.floor.rooms.values()])
        store.close()
    assert names[0] == names[1]


def test_floor_is_persisted_to_the_graph(game):
    """Read back by `load_floor_identity` since M6, which is what makes floors
    persist. Written in `begin()` rather than `new_run` so the director has
    already run and the floor node carries its real name."""
    engine, state, store = game
    list(engine.begin())

    floor_node = f"floor:{state.depth}"
    rooms = store.neighbors(floor_node, "CONTAINS")
    assert len(rooms) == len(state.floor.rooms)

    entrance = f"room:{state.floor.entrance_id}"
    exits = [n for n in store.neighbors(entrance) if n["rel"].startswith("EXIT_")]
    assert len(exits) == len(state.floor.room(state.floor.entrance_id).exits)

    assert store.neighbors(f"run:{state.run_id}", "ENTERED")
