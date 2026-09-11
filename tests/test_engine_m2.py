"""Engine wiring for M2: prose events, the director beat, prefetch scheduling."""

from __future__ import annotations

import pytest

from simulacra.config import Settings
from simulacra.engine.events import Line, ProseDelta, ProseEnd, ProseStart, Thinking
from simulacra.engine.loop import Engine
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.narrate.narrator import Narrator
from simulacra.narrate.prefetch import Prefetcher
from simulacra.world.model import RoomKind

from conftest import FakeClient

GOOD = "The floor slopes away from you, and the water knows it."


@pytest.fixture
def wired(tmp_path, theme):
    store = Store(tmp_path / "m2.db")
    settings = Settings()
    client = FakeClient(script=[GOOD], structured_result={
        "theme_name": "The Reprinted Ward", "goal": "Find the master copy.",
        "motifs": ["ink"], "rooms": [],
    })
    narrator = Narrator(client, theme, store, settings.narrator)
    prefetcher = Prefetcher(narrator, store, enabled=False)  # deterministic
    state = new_run(store, theme, settings, seed=42)
    engine = Engine(state, store, settings, theme,
                    narrator=narrator, prefetcher=prefetcher, client=client)
    yield engine, state, client, store
    prefetcher.stop()
    store.close()


def collect(gen):
    return list(gen)


def test_describe_streams_prose_events(wired):
    engine, _, _, _ = wired
    events = collect(engine.begin())
    types = [type(e) for e in events]

    assert ProseStart in types and ProseEnd in types
    assert types.count(ProseDelta) > 1, "prose arrived as one lump, not a stream"
    text = "".join(e.text for e in events if isinstance(e, ProseDelta))
    assert text == GOOD


def test_prose_start_precedes_deltas_which_precede_end(wired):
    engine, _, _, _ = wired
    types = [type(e) for e in collect(engine.begin())]
    assert types.index(ProseStart) < types.index(ProseDelta) < types.index(ProseEnd)


def test_director_runs_behind_a_thinking_beat(wired):
    engine, state, _, _ = wired
    events = collect(engine.begin())
    assert any(isinstance(e, Thinking) for e in events)
    assert state.floor.theme_name == "The Reprinted Ward"


def test_floor_history_grows_so_the_director_can_avoid_repeating(wired):
    engine, state, _, _ = wired
    collect(engine.begin())
    assert state.floor_history == ["The Reprinted Ward"]


def test_descent_directs_the_new_floor(wired):
    engine, state, _, _ = wired
    collect(engine.begin())
    descent = next(r for r in state.floor.rooms.values() if r.kind is RoomKind.DESCENT)
    state.room_id = descent.id

    events = collect(engine.turn("descend"))
    assert any(isinstance(e, Thinking) for e in events)
    assert state.depth == 2
    # The fake director answers every floor identically, and M12 refuses a
    # floor name the world already uses -- floor 2 takes the theme's title.
    assert state.floor.theme_name == engine.theme.floor_title(2)


def test_offline_engine_still_emits_flat_lines(tmp_path, theme):
    """The M1 path is not dead code -- it is the no-model fallback."""
    store = Store(tmp_path / "off.db")
    settings = Settings()
    state = new_run(store, theme, settings, seed=42)
    engine = Engine(state, store, settings, theme)

    types = [type(e) for e in collect(engine.begin())]
    assert Line in types
    assert ProseStart not in types
    assert Thinking not in types
    store.close()


def test_entering_a_room_schedules_only_its_neighbours(tmp_path, theme):
    store = Store(tmp_path / "sched.db")
    settings = Settings()
    client = FakeClient(script=[GOOD], structured_result={
        "theme_name": "X", "goal": "", "motifs": [], "rooms": []})
    narrator = Narrator(client, theme, store, settings.narrator)

    scheduled: list[list[str]] = []

    class Recording(Prefetcher):
        def schedule(self, floor, room_ids):
            scheduled.append(list(room_ids))

    pf = Recording(narrator, store, enabled=True)
    state = new_run(store, theme, settings, seed=42)
    engine = Engine(state, store, settings, theme,
                    narrator=narrator, prefetcher=pf, client=client)

    collect(engine.begin())
    entrance = state.floor.room(state.floor.entrance_id)
    assert scheduled and set(scheduled[0]) == set(entrance.exits.values())
    store.close()


def test_foreground_preempts_the_prefetcher_on_arrival(tmp_path, theme):
    store = Store(tmp_path / "pre.db")
    settings = Settings()
    client = FakeClient(script=[GOOD], structured_result={
        "theme_name": "X", "goal": "", "motifs": [], "rooms": []})
    narrator = Narrator(client, theme, store, settings.narrator)

    order: list[str] = []

    class Recording(Prefetcher):
        def preempt(self):
            order.append("preempt")

        def schedule(self, floor, room_ids):
            order.append("schedule")

    pf = Recording(narrator, store, enabled=True)
    state = new_run(store, theme, settings, seed=42)
    engine = Engine(state, store, settings, theme,
                    narrator=narrator, prefetcher=pf, client=client)

    collect(engine.begin())
    # Take the model back before narrating, hand it over again afterwards.
    assert order == ["preempt", "schedule"]
    store.close()
