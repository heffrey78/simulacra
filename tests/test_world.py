"""M6: the world persists.

The claim under test is that floor 7 is the same floor 7 on every run of a
world, that its layout owes nothing to the dice, and that a world can be
archived or partially forgotten rather than only deleted.
"""

from __future__ import annotations

import sqlite3

import pytest

from simulacra.config import Settings
from simulacra.engine.loop import Engine
from simulacra.engine.state import new_run
from simulacra.memory.store import (
    SCHEMA_VERSION,
    Store,
    WorldVersionError,
    archive_world,
)
from simulacra.narrate.narrator import Narrator
from simulacra.world.floorgen import floor_rng, generate_floor

from conftest import FakeClient

PLAN = {
    "theme_name": "The Reprinted Ward",
    "goal": "Find the master copy.",
    "motifs": ["ink", "damp"],
    "rooms": [],
}

# Handed to run 2's client. If the director runs again, the floor takes *this*
# identity -- so asserting run 2 still sees PLAN proves the identity came out of
# the store rather than out of a second generation that happened to agree.
OTHER = {
    "theme_name": "The Salt Stair",
    "goal": "Something else entirely.",
    "motifs": ["salt"],
    "rooms": [],
}


def shape(floor):
    """Everything about a floor that generation decides."""
    return sorted(
        (r.id, r.kind.value, r.name, tuple(sorted((d.value, x) for d, x in r.exits.items())))
        for r in floor.rooms.values()
    )


# -- the world row ---------------------------------------------------------


def test_a_new_store_has_exactly_one_world(tmp_path):
    store = Store(tmp_path / "w.db", theme="simulacra", world_seed=7)
    row = store.world()
    assert row["world_seed"] == 7
    assert row["theme"] == "simulacra"
    assert row["schema_version"] == SCHEMA_VERSION
    assert store.db.execute("SELECT COUNT(*) AS n FROM world").fetchone()["n"] == 1
    store.close()


def test_reopening_never_rewrites_the_world(tmp_path):
    Store(tmp_path / "w.db", theme="simulacra", world_seed=7).close()
    store = Store(tmp_path / "w.db", theme="other", world_seed=999)
    assert store.world()["world_seed"] == 7
    assert store.world()["theme"] == "simulacra"
    store.close()


def test_the_stored_theme_is_the_one_dash_dash_theme_accepts(theme):
    """The world records the pack stem, not the display name -- otherwise the
    theme-mismatch refusal suggests a --theme argument that does not resolve."""
    assert theme.pack == "simulacra"
    assert theme.name == "Simulacra"


def test_a_world_with_no_theme_yet_claims_one_on_first_real_open(tmp_path):
    """A bare Store() -- every test in the suite -- must not have to know the
    world table exists. The theme is claimed when the game finally opens it."""
    Store(tmp_path / "w.db").close()
    store = Store(tmp_path / "w.db", theme="simulacra")
    assert store.world()["theme"] == "simulacra"
    store.close()


def test_a_future_schema_version_is_refused_by_name(tmp_path):
    path = tmp_path / "w.db"
    Store(path).close()
    db = sqlite3.connect(path)
    db.execute("UPDATE world SET schema_version = ?", (SCHEMA_VERSION + 1,))
    db.commit()
    db.close()

    with pytest.raises(WorldVersionError) as e:
        Store(path)
    assert "--new-world" in str(e.value), "the refusal must name the way out"


def test_a_pre_m6_world_is_refused_rather_than_migrated(tmp_path):
    """A POC-era file has runs but no world table. Its rooms were generated from
    a throwaway per-run seed, so its room ids name rooms that no longer exist."""
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at REAL)")
    db.execute("INSERT INTO runs (started_at) VALUES (1.0)")
    db.commit()
    db.close()

    with pytest.raises(WorldVersionError) as e:
        Store(path)
    assert "--new-world" in str(e.value)


def test_an_empty_file_is_not_mistaken_for_a_pre_m6_world(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    Store(path).close()  # must not raise


# -- the seed split --------------------------------------------------------


def test_a_seed_claims_an_unplayed_world(tmp_path):
    store = Store(tmp_path / "w.db")
    assert store.claim_world_seed(42) == 42
    assert store.world()["world_seed"] == 42
    store.close()


def test_a_seed_offered_to_a_played_world_is_not_the_world_seed(tmp_path, theme):
    """--seed must not silently re-shape floors the player has already walked."""
    store = Store(tmp_path / "w.db", world_seed=1)
    new_run(store, theme, Settings(), seed=1)
    assert store.claim_world_seed(999) == 1
    assert store.world()["world_seed"] == 1
    store.close()


def test_two_runs_of_one_world_walk_the_same_floors(tmp_path, theme):
    store = Store(tmp_path / "w.db", world_seed=4242)
    settings = Settings()

    first = new_run(store, theme, settings, seed=1)
    second = new_run(store, theme, settings, seed=2)
    assert shape(first.floor) == shape(second.floor)

    for depth in range(1, 6):
        a = generate_floor(depth, theme, floor_rng(first.world_seed, depth))
        b = generate_floor(depth, theme, floor_rng(second.world_seed, depth))
        assert shape(a) == shape(b), f"depth {depth} differs between runs"
    store.close()


def test_different_worlds_are_different_dungeons(tmp_path, theme):
    a = generate_floor(3, theme, floor_rng(1, 3))
    b = generate_floor(3, theme, floor_rng(2, 3))
    assert shape(a) != shape(b)


def test_adjacent_depths_are_not_the_same_floor(tmp_path, theme):
    """`world_seed ^ depth` would make neighbouring depths differ by one bit."""
    shapes = [shape(generate_floor(d, theme, floor_rng(99, d))) for d in (4, 5, 6)]
    assert len({str(s) for s in shapes}) == 3


def test_floor_layout_owes_nothing_to_the_dice(tmp_path, theme):
    """The M6 regression test. `descend()` used to generate from `state.rng` --
    the dice stream -- so how many attacks you rolled on floor 1 decided the
    shape of floor 2."""
    settings = Settings()

    def floor_two(rolls: int):
        store = Store(tmp_path / f"dice{rolls}.db", world_seed=77)
        state = new_run(store, theme, settings, seed=5)
        engine = Engine(state, store, settings, theme)
        list(engine.begin())
        for _ in range(rolls):
            state.rng.random()
        state.room_id = next(
            r.id for r in state.floor.rooms.values() if r.kind.value == "descent"
        )
        list(engine.descend())
        out = shape(state.floor)
        store.close()
        return out

    assert floor_two(0) == floor_two(50)


def test_the_run_seed_is_recorded_for_replay(tmp_path, theme):
    store = Store(tmp_path / "w.db", world_seed=3)
    state = new_run(store, theme, Settings(), seed=808)
    row = store.db.execute("SELECT seed FROM runs WHERE id = ?", (state.run_id,)).fetchone()
    assert row["seed"] == 808 and state.seed == 808
    store.close()


# -- identity is stored, layout is recomputed ------------------------------


@pytest.fixture
def directed(tmp_path, theme):
    """A world whose floor 1 has been through the director exactly once."""
    settings = Settings()
    store = Store(tmp_path / "w.db", world_seed=2024)
    client = FakeClient(script=["Wet stone, and a draught from somewhere."],
                        structured_result=PLAN)
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator),
                    client=client)
    list(engine.begin())
    yield tmp_path, theme, settings, store, client, state
    store.close()


def test_the_director_runs_once_on_a_fresh_floor(directed):
    _, _, _, _, client, _ = directed
    assert client.calls.count("tier3") == 1


def test_a_second_run_pays_no_director_call(directed):
    """The payoff of persisting floors: the ~19 s tier-3 call is paid once per
    floor for the life of the world, not once per floor per run."""
    tmp_path, theme, settings, store, _, _ = directed

    client2 = FakeClient(script=["ignored"], structured_result=OTHER)
    state2 = new_run(store, theme, settings, seed=2)
    engine2 = Engine(state2, store, settings, theme,
                     narrator=Narrator(client2, theme, store, settings.narrator),
                     client=client2)
    list(engine2.begin())

    assert client2.calls.count("tier3") == 0, "the director ran again on a known floor"
    assert state2.floor.theme_name == PLAN["theme_name"]
    assert state2.floor.goal == PLAN["goal"]
    assert state2.floor.motifs == tuple(PLAN["motifs"])


def test_floor_one_gets_its_real_name_in_the_graph(directed):
    """Before M6 floor 1 was persisted in `new_run`, before the director ran, so
    `floor:1` was named "floor 1" forever while deeper floors got real names."""
    _, _, _, store, _, _ = directed
    assert store.node("floor:1")["name"] == PLAN["theme_name"]


def test_room_prose_survives_across_runs(directed):
    """Stable concepts make `Narrator._key` stable, which makes the prose cache
    outlive a run. If this fails, W3's free win is not landing."""
    tmp_path, theme, settings, store, _, _ = directed

    client2 = FakeClient(script=["DIFFERENT TEXT"], structured_result=OTHER)
    state2 = new_run(store, theme, settings, seed=2)
    engine2 = Engine(state2, store, settings, theme,
                     narrator=Narrator(client2, theme, store, settings.narrator),
                     client=client2)
    list(engine2.begin())
    assert client2.stream_calls == 0, "the entrance was re-narrated from scratch"


def test_the_avoid_list_comes_from_the_world_not_the_traversal(directed):
    """Run 2's first descent has walked nothing, and would otherwise tell the
    director to avoid nothing while the world already uses those names."""
    _, _, _, store, _, _ = directed
    assert store.floor_names(below_depth=2) == [PLAN["theme_name"]]
    assert store.floor_names(below_depth=1) == []


def test_structural_edges_do_not_multiply_per_run(directed):
    """A wall between two rooms is true of the world, not of one run."""
    tmp_path, theme, settings, store, _, _ = directed
    before = store.db.execute("SELECT COUNT(*) AS n FROM edges WHERE rel LIKE 'EXIT_%'").fetchone()["n"]

    state2 = new_run(store, theme, settings, seed=2)
    list(Engine(state2, store, settings, theme).begin())

    after = store.db.execute("SELECT COUNT(*) AS n FROM edges WHERE rel LIKE 'EXIT_%'").fetchone()["n"]
    assert after == before


# -- reset -----------------------------------------------------------------


def test_archive_moves_the_world_aside(tmp_path):
    path = tmp_path / "world.db"
    Store(path, world_seed=5).close()
    dest = archive_world(path)

    assert dest is not None and dest.exists()
    assert not path.exists()
    assert Store(dest).world()["world_seed"] == 5


def test_archiving_nothing_is_not_an_error(tmp_path):
    assert archive_world(tmp_path / "absent.db") is None


def test_a_second_archive_does_not_overwrite_the_first(tmp_path):
    path = tmp_path / "world.db"
    Store(path, world_seed=1).close()
    first = archive_world(path)
    Store(path, world_seed=2).close()
    second = archive_world(path)

    assert first != second
    assert Store(first).world()["world_seed"] == 1
    assert Store(second).world()["world_seed"] == 2


def test_archive_takes_the_wal_siblings_with_it(tmp_path):
    path = tmp_path / "world.db"
    store = Store(path, world_seed=1)
    store.remember(1, "something worth keeping")  # forces a WAL write
    dest = archive_world(path)
    assert not (tmp_path / "world.db-wal").exists()
    assert Store(dest).db.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 1


def test_forget_removes_one_npcs_memories_and_no_one_elses(tmp_path):
    store = Store(tmp_path / "w.db")
    store.remember(1, "the archivist watched a delver die", subjects=["npc:archivist"])
    store.remember(1, "the warden watched a different one", subjects=["npc:warden"])
    store.commit()

    assert store.forget("npc:archivist") == (1, 0)
    left = [r["text"] for r in store.db.execute("SELECT text FROM memories")]
    assert left == ["the warden watched a different one"]
    store.close()


def test_forget_keeps_a_memory_another_node_still_claims(tmp_path):
    """A death is subject to both the NPC who witnessed it and the room it
    happened in. Forgetting the NPC must not erase the room's history."""
    store = Store(tmp_path / "w.db")
    store.remember(1, "a delver died here", subjects=["npc:archivist", "room:d1r3"])
    store.commit()

    assert store.forget("npc:archivist") == (0, 0)
    assert store.recall(about="room:d1r3", limit=3)[0].text == "a delver died here"
    assert store.recall(about="npc:archivist", limit=3) == []
    store.close()


def test_forget_takes_the_vector_with_the_memory(tmp_path):
    store = Store(tmp_path / "w.db")
    store.remember(1, "gone", subjects=["npc:archivist"], embedding=[0.1] * 768)
    store.commit()
    store.forget("npc:archivist")
    n = store.db.execute("SELECT COUNT(*) AS n FROM memory_vectors").fetchone()["n"]
    assert n == 0, "an orphaned vector would still be returned by an unanchored recall"
    store.close()
