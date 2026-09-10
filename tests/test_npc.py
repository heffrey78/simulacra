"""Persistent NPCs: placement, and the non-hostile path combat never exercised.

Before M4 nothing in the game had `hostile=False`, so the filters in
`actors_attack` and `_attack` were untested by construction.
"""

from __future__ import annotations

import random

import pytest

from simulacra.config import Settings
from simulacra.engine.combat import actors_attack
from simulacra.engine.events import Notice, NpcPresent, ProseDelta, Roll, Transcript
from simulacra.engine.loop import Engine
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.narrate.narrator import Narrator
from simulacra.world.floorgen import generate_floor
from simulacra.world.model import Player
from simulacra.world.theme import Theme

from conftest import FakeClient

REPLY = "Two hundred and nine went down."


def npcs_on(floor):
    return [a for r in floor.rooms.values() for a in r.actors if not a.hostile]


def resident(theme, depth):
    """Roster entries that live on this floor. Since M7 `depth` is per-NPC, so
    "the NPC floor" is no longer one floor."""
    return [n for n in theme.npcs if n.depth == depth]


def home_depth(theme):
    return theme.npcs[0].depth


# -- placement -------------------------------------------------------------


@pytest.mark.parametrize("depth", sorted({n.depth for n in Theme.load("simulacra").npcs}))
def test_each_npc_is_placed_on_its_own_floor(theme, depth):
    found = npcs_on(generate_floor(depth, theme, random.Random(1)))
    assert {a.id for a in found} == {n.anchor for n in resident(theme, depth)}


def test_the_roster_does_not_all_live_on_one_floor(theme):
    """C2 added a second NPC at a different depth specifically so that canon
    anchoring is falsifiable -- with one NPC every anchor test passes whether
    the anchoring works or not."""
    assert len({n.depth for n in theme.npcs}) > 1


def test_the_actor_id_is_the_theme_anchor(theme):
    """A generated id per run would make a new NPC every time and nothing would
    ever be remembered."""
    a = npcs_on(generate_floor(home_depth(theme), theme, random.Random(1)))[0]
    assert a.id.startswith("npc:")
    assert a.id in {n.anchor for n in theme.npcs}


@pytest.mark.parametrize("seed", range(8))
def test_placement_is_stable_across_seeds(theme, seed):
    depth = home_depth(theme)
    assert len(npcs_on(generate_floor(depth, theme, random.Random(seed)))) == len(
        resident(theme, depth)
    )


@pytest.mark.parametrize("depth", [2, 4, 5, 9])
def test_no_npc_appears_on_a_floor_that_is_not_theirs(theme, depth):
    assert [n for n in theme.npcs if n.depth == depth] == [], "pick an unoccupied depth"
    assert npcs_on(generate_floor(depth, theme, random.Random(1))) == []


def test_nothing_hostile_shares_the_npc_room(theme):
    for seed in range(20):
        floor = generate_floor(home_depth(theme), theme, random.Random(seed))
        host = next(r for r in floor.rooms.values()
                    if any(not a.hostile for a in r.actors))
        assert not any(a.hostile for a in host.actors), f"seed {seed}: a monster shares the room"


# -- safety ----------------------------------------------------------------


def test_npcs_never_attack_the_player(theme):
    npc = npcs_on(generate_floor(home_depth(theme), theme, random.Random(1)))[0]
    player = Player()
    assert actors_attack([npc], player, random.Random(0)) == []
    assert player.hp == player.max_hp


@pytest.fixture
def game(tmp_path, theme):
    store = Store(tmp_path / "npc.db")
    settings = Settings()
    client = FakeClient(script=[REPLY])
    narrator = Narrator(client, theme, store, settings.narrator)
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme, narrator=narrator, client=client)
    list(engine.begin())
    host = next(r for r in state.floor.rooms.values()
                if any(not a.hostile for a in r.actors))
    state.room_id = host.id
    yield engine, state, store, client
    store.close()


def test_you_cannot_attack_the_archivist(game):
    """Still true: an NPC is never rolled against and never hurt. Since M11 the
    attempt is no longer free, though -- it is the wary band's only door."""
    from simulacra.engine import dealings

    engine, state, store, _ = game
    events = list(engine.turn("attack archivist"))
    assert not any(isinstance(e, Roll) for e in events)
    assert all(a.hp > 0 for a in state.room.actors)
    assert dealings.is_wary(store, "npc:archivist")


def test_entering_the_room_announces_them_as_a_person_not_a_threat(game):
    engine, state, _, _ = game
    events = list(engine.turn("look"))
    assert any(isinstance(e, NpcPresent) for e in events)


# -- conversation ----------------------------------------------------------


def test_talking_streams_a_reply(game):
    engine, _, _, _ = game
    events = list(engine.turn("talk to archivist"))
    assert any(isinstance(e, NpcPresent) for e in events)
    text = "".join(e.text for e in events if isinstance(e, ProseDelta))
    assert text == REPLY


def test_talking_records_the_conversation_for_next_time(game):
    engine, _, _, _ = game
    transcripts = [e for e in engine.turn("talk to archivist") if isinstance(e, Transcript)]
    assert transcripts and transcripts[0].kind == "dialogue"
    assert "npc:archivist" in transcripts[0].subjects


def test_talking_marks_the_npc_as_met(game):
    engine, state, _, _ = game
    list(engine.turn("talk to archivist"))
    assert "npc:archivist" in state.met_npcs


def test_talking_to_nobody_is_refused(game):
    engine, state, _, _ = game
    state.room.actors.clear()
    assert any(isinstance(e, Notice) for e in engine.turn("talk"))


def test_offline_talking_degrades_rather_than_crashing(tmp_path, theme):
    store = Store(tmp_path / "off.db")
    settings = Settings()
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme)  # no narrator, no client
    list(engine.begin())
    host = next(r for r in state.floor.rooms.values()
                if any(not a.hostile for a in r.actors))
    state.room_id = host.id
    assert any(isinstance(e, Notice) for e in engine.turn("talk"))
    store.close()


def test_the_npc_never_records_its_own_words(game):
    """Storing the reply feeds an NPC its own words next run, and the loop
    compounds -- three runs in, recall was self-quotation crowding out a death."""
    engine, _, _, _ = game
    t = next(e for e in engine.turn("talk to archivist") if isinstance(e, Transcript))
    assert REPLY not in t.summary
    assert "the Archivist said" not in t.summary
    assert "archivist" in t.summary.lower()  # still about them
