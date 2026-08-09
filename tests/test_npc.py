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
from simulacra.world.floorgen import NPC_DEPTH, generate_floor
from simulacra.world.model import Player

from conftest import FakeClient

REPLY = "Two hundred and nine went down."


def npcs_on(floor):
    return [a for r in floor.rooms.values() for a in r.actors if not a.hostile]


# -- placement -------------------------------------------------------------


def test_the_roster_is_placed_on_the_npc_floor(theme):
    found = npcs_on(generate_floor(NPC_DEPTH, theme, random.Random(1)))
    assert {a.id for a in found} == {n.anchor for n in theme.npcs}


def test_the_actor_id_is_the_theme_anchor(theme):
    """A generated id per run would make a new NPC every time and nothing would
    ever be remembered."""
    a = npcs_on(generate_floor(NPC_DEPTH, theme, random.Random(1)))[0]
    assert a.id.startswith("npc:")
    assert a.id in {n.anchor for n in theme.npcs}


@pytest.mark.parametrize("seed", range(8))
def test_placement_is_stable_across_seeds(theme, seed):
    assert len(npcs_on(generate_floor(NPC_DEPTH, theme, random.Random(seed)))) == len(theme.npcs)


@pytest.mark.parametrize("depth", [2, 3, 5, 9])
def test_npcs_do_not_appear_on_deeper_floors(theme, depth):
    assert npcs_on(generate_floor(depth, theme, random.Random(1))) == []


def test_nothing_hostile_shares_the_npc_room(theme):
    for seed in range(20):
        floor = generate_floor(NPC_DEPTH, theme, random.Random(seed))
        host = next(r for r in floor.rooms.values()
                    if any(not a.hostile for a in r.actors))
        assert not any(a.hostile for a in host.actors), f"seed {seed}: a monster shares the room"


# -- safety ----------------------------------------------------------------


def test_npcs_never_attack_the_player(theme):
    npc = npcs_on(generate_floor(NPC_DEPTH, theme, random.Random(1)))[0]
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
    engine, state, _, _ = game
    events = list(engine.turn("attack archivist"))
    assert any(isinstance(e, Notice) for e in events)
    assert not any(isinstance(e, Roll) for e in events)
    assert all(a.hp > 0 for a in state.room.actors)


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
