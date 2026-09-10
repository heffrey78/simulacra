"""M10: the room remembers what it showed you.

The failure this milestone exists to fix is the engine contradicting the player:
the narrator writes "a cracked altar stands against the far wall", and `look at
the altar` answers "You don't see altar here."
"""

from __future__ import annotations

import pytest

from simulacra.config import Settings
from simulacra.engine.events import Notice, ProseDelta
from simulacra.engine.loop import Engine
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.narrate.narrator import Narrator
from simulacra.world import discovery
from simulacra.world.model import RoomKind

from conftest import FakeClient

FOUND = "A cracked altar, its top worn smooth by hands that are no longer here."


class Looking(FakeClient):
    def __init__(self, script=None):
        super().__init__(script=script or [FOUND])
        self.streams = 0

    def stream(self, messages, policy, *, kind="stream"):
        self.streams += 1
        yield from super().stream(messages, policy, kind=kind)


@pytest.fixture
def room(tmp_path, theme):
    """A shrine whose prose mentions an altar, and nothing else."""
    settings = Settings()
    store = Store(tmp_path / "w.db", world_seed=7)
    client = Looking()
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator),
                    client=client)
    list(engine.begin())

    here = state.room
    here.concept = "A room kept in better repair than the rest."
    here.prose = "A cracked altar stands against the far wall."
    client.streams = 0
    yield engine, state, store, client, here
    store.close()


def said(events) -> str:
    return "".join(e.text for e in events if isinstance(e, ProseDelta))


# -- D2: grading, in code --------------------------------------------------


def test_a_noun_the_prose_used_is_mentioned(room, theme):
    _, state, _, _, here = room
    assert discovery.grade("the altar", here, theme, world_seed=7) == "mentioned"


def test_a_noun_the_concept_used_is_mentioned(room, theme):
    _, _, _, _, here = room
    assert discovery.grade("repair", here, theme, world_seed=7) == "mentioned"


def test_a_word_in_neither_is_absent(room, theme):
    _, _, _, _, here = room
    assert discovery.grade("chandelier", here, theme, world_seed=7) == "absent"


@pytest.mark.parametrize("junk", [
    "the", "a", "it", "", "  ", "an",
    # Measured live: `look at the under` found "a patch of moss and dust",
    # because a preposition passed the length floor and appeared in the prose.
    "under", "against", "through", "were", "would", "always",
])
def test_function_words_find_nothing(room, theme, junk):
    _, _, _, _, here = room
    here.prose = "The altar stands under the arch, against a wall it would always outlast."
    assert discovery.grade(junk, here, theme, world_seed=7) == "absent"


def test_short_targets_do_not_match_everything(room, theme):
    """A two-letter target is not a search."""
    _, _, _, _, here = room
    assert discovery.grade("ar", here, theme, world_seed=7) == "absent"


def test_matching_is_by_word_not_substring(room, theme):
    _, _, _, _, here = room
    here.prose = "The alteration was never finished."
    assert discovery.grade("altar", here, theme, world_seed=7) == "absent"


def test_fertility_is_a_property_of_the_room(theme):
    """Rolled per room, not per look -- otherwise a player types the same thing
    four times until it lands, which is the grind the budget exists to prevent."""
    seen = {discovery.is_fertile("d1r3", 7) for _ in range(20)}
    assert len(seen) == 1

    both = {discovery.is_fertile(f"d1r{i}", 7) for i in range(40)}
    assert both == {True, False}, "every room fertile, or none, is not a roll"


def test_a_fixture_needs_a_fertile_room(theme):
    from simulacra.world.model import Room

    fertile = next(f"d9r{i}" for i in range(40) if discovery.is_fertile(f"d9r{i}", 7))
    barren = next(f"d9r{i}" for i in range(40) if not discovery.is_fertile(f"d9r{i}", 7))

    def shrine(rid):
        return Room(id=rid, kind=RoomKind.SHRINE, depth=1, name="the Original")

    assert discovery.grade("altar", shrine(fertile), theme, world_seed=7) == "fixture"
    assert discovery.grade("altar", shrine(barren), theme, world_seed=7) == "absent"


# -- D3: finding it --------------------------------------------------------


def test_looking_at_what_the_prose_described_finds_it(room):
    engine, _, store, client, here = room
    events = list(engine.turn("look at the altar"))

    assert FOUND in said(events)
    assert client.streams == 1
    assert [r["text"] for r in store.canon(f"room:{here.id}")] == [FOUND]


def test_a_discovery_is_an_action_and_a_look_is_not(room):
    """Eyeballing stays free. Rummaging until you turn something up takes a
    turn, and doing it with something hostile in the room should cost you."""
    engine, _, _, _, _ = room
    list(engine.turn("look"))
    assert engine._resolved is False

    list(engine.turn("look at the altar"))
    assert engine._resolved is True


def test_a_rejected_generation_is_never_written_down(room):
    """Canon outlives the run. M7 paid a live five-run read to learn this."""
    engine, _, store, client, here = room
    client.script = ["ROOM: the Original"]  # trips the echo guard

    list(engine.turn("look at the altar"))
    assert store.canon(f"room:{here.id}") == []


def test_a_word_the_room_never_used_is_still_refused(room):
    engine, _, _, client, _ = room
    events = list(engine.turn("look at the chandelier"))
    assert any(isinstance(e, Notice) for e in events)
    assert client.streams == 0


# -- D4: the same thing next time ------------------------------------------


def test_a_second_look_costs_nothing_and_says_the_same(room):
    engine, _, _, client, _ = room
    first = said(list(engine.turn("look at the altar")))
    client.script = ["SOMETHING COMPLETELY DIFFERENT"]

    second = said(list(engine.turn("look at the altar")))
    assert second == first
    assert client.streams == 1, "a repeat look re-generated"


def test_what_a_room_showed_you_survives_the_run(room):
    """With floors persisting since M6, a world that rewards re-exploration has
    to remember what it showed you."""
    engine, state, store, client, here = room
    list(engine.turn("look at the altar"))

    later = new_run(store, engine.theme, Settings(), seed=99)
    engine2 = Engine(later, store, Settings(), engine.theme,
                     narrator=engine.narrator, client=client)
    later.room_id = here.id
    client.script = ["SOMETHING COMPLETELY DIFFERENT"]

    assert FOUND in said(list(engine2.turn("look at the altar")))
    assert client.streams == 1


def test_a_discovery_is_not_takeable(room):
    """Scenery, not treasure. The moment it becomes an item it is free healing
    on demand, and M10 turns into a loot-generation milestone."""
    engine, state, _, _, _ = room
    before = list(state.player.inventory)
    list(engine.turn("look at the altar"))
    assert state.player.inventory == before
    assert not any("altar" in i.name.lower() for i in state.room.items)


# -- D5: exhaustion --------------------------------------------------------


def test_a_room_runs_out(room):
    engine, _, store, client, here = room
    for i in range(discovery.DISCOVERY_BUDGET):
        store.add_canon(f"room:{here.id}", f"Something already found, number {i}.",
                        "derived")
    store.commit()
    client.streams = 0

    events = list(engine.turn("look at the altar"))
    assert any(isinstance(e, Notice) for e in events)
    assert client.streams == 0, "an exhausted room still spent a model call"


def test_an_exhausted_room_still_shows_what_it_showed_before(room):
    engine, _, store, client, here = room
    list(engine.turn("look at the altar"))
    for i in range(discovery.DISCOVERY_BUDGET):
        store.add_canon(f"room:{here.id}", f"Filler number {i}.", "derived")
    store.commit()
    client.streams = 0

    assert FOUND in said(list(engine.turn("look at the altar")))
    assert client.streams == 0


def test_the_budget_is_per_room(room):
    engine, state, store, client, here = room
    for i in range(discovery.DISCOVERY_BUDGET):
        store.add_canon(f"room:{here.id}", f"Filler number {i}.", "derived")
    store.commit()

    elsewhere = next(r for r in state.floor.rooms.values() if r.id != here.id)
    elsewhere.prose = "A cracked altar stands against the far wall."
    state.room_id = elsewhere.id
    client.streams = 0

    assert FOUND in said(list(engine.turn("look at the altar")))
    assert client.streams == 1


def test_discovery_degrades_without_a_model(tmp_path, theme):
    settings = Settings()
    store = Store(tmp_path / "off.db", world_seed=7)
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme)  # no narrator, no client
    list(engine.begin())
    state.room.prose = "A cracked altar stands against the far wall."

    assert any(isinstance(e, Notice) for e in engine.turn("look at the altar"))
    store.close()


# -- the two the live run found --------------------------------------------


def test_the_prose_the_player_read_is_recorded_on_the_room(tmp_path, theme):
    """`Room.prose` and `Room.described` were declared in M1 and nothing ever
    wrote to them -- the text lived only in `prose_cache`, keyed by a hash. So
    grading against the prose was grading against an empty string, which is the
    exact gap this milestone's own Watch-for predicted."""
    settings = Settings()
    store = Store(tmp_path / "w.db", world_seed=7)
    client = Looking(script=["A cracked altar stands against the far wall."])
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator),
                    client=client)
    list(engine.begin())

    assert state.room.prose == "A cracked altar stands against the far wall."
    assert state.room.described
    store.close()


def test_a_noun_only_the_prose_used_is_findable(tmp_path, theme):
    settings = Settings()
    store = Store(tmp_path / "w.db", world_seed=7)
    client = Looking(script=["A cracked altar stands against the far wall."])
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator),
                    client=client)
    list(engine.begin())
    state.room.concept = ""  # the concept says nothing; only the prose does

    assert discovery.grade("altar", state.room, theme, world_seed=7) == "mentioned"
    store.close()


def test_a_repeat_look_is_keyed_on_what_was_searched(room):
    """Measured live: `look at the surface` replayed the door, because the
    door's description happened to use the word "surface"."""
    engine, _, _, client, _ = room
    list(engine.turn("look at the altar"))
    assert FOUND in said(list(engine.turn("look at the altar")))

    # "smooth" appears in FOUND but was never searched for, and is in neither
    # the prose nor the fixture pool.
    events = list(engine.turn("look at the smooth"))
    assert any(isinstance(e, Notice) for e in events)


def test_every_word_of_the_search_reaches_the_find(room):
    engine, _, _, client, _ = room
    list(engine.turn("look at the cracked altar"))
    client.script = ["SOMETHING COMPLETELY DIFFERENT"]

    assert FOUND in said(list(engine.turn("look at the altar")))
    assert FOUND in said(list(engine.turn("look at the cracked")))
    assert client.streams == 1
