"""M14.1: the third playtest, answered (docs/playtests/2026-09-11.md).

The first play of M14, and the first on qwen3.5:2b. Every test names the line
of the transcript it answers.
"""

from __future__ import annotations

import io
import random
from types import SimpleNamespace

import pytest
from rich.console import Console

from simulacra.config import Settings
from simulacra.engine import dealings
from simulacra.engine.events import FloorNamed, Line, Notice, NpcPresent, ProseDelta
from simulacra.engine.loop import Engine
from simulacra.engine.routes import as_speaker
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.narrate.narrator import Narrator, recites_itself
from simulacra.ui.repl import ReplRenderer
from simulacra.world.director import apply_floor_plan
from simulacra.world.floorgen import floor_rng, generate_floor
from simulacra.world.model import Item, RoomKind
from simulacra.world.theme import Theme

from conftest import FakeClient

PLAYTEST_WORLD = 294673103


def texts(events) -> list[str]:
    return [getattr(e, "text", "") for e in events]


@pytest.fixture
def game(tmp_path, theme):
    settings = Settings()
    store = Store(tmp_path / "w.db")
    state = new_run(store, theme, settings, seed=7)
    engine = Engine(state, store, settings, theme)
    list(engine.begin())
    yield engine, state, store
    store.close()


@pytest.fixture
def archivist(tmp_path, theme):
    """Standing with the Archivist, a model that says one thing."""
    settings = Settings()
    store = Store(tmp_path / "w.db")
    client = FakeClient(script=["Two hundred and nine went down."])
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator), client=client)
    list(engine.begin())
    state.room_id = next(r.id for r in state.floor.rooms.values()
                         if any(a.id == "npc:archivist" for a in r.actors))
    yield engine, state, store
    store.close()


def looted(theme, ws, depth):
    """A floor as the game builds it: its own stream, then the vault's and the loot's."""
    return generate_floor(depth, theme, floor_rng(ws, depth),
                          loot_rng=floor_rng(ws, depth, "loot"),
                          vault_rng=floor_rng(ws, depth, "vault"))


def test_the_vault_stream_does_not_move_the_floor(theme):
    """Existing worlds keep their layout, monsters and items; the vault-less
    weapon is only ever added after them."""
    from test_loot import items_extend, shape
    for ws in (1, 42, PLAYTEST_WORLD):
        for depth in range(1, 9):
            plain = generate_floor(depth, theme, floor_rng(ws, depth))
            armed = generate_floor(depth, theme, floor_rng(ws, depth),
                                   vault_rng=floor_rng(ws, depth, "vault"))
            assert shape(plain) == shape(armed)
            assert items_extend(plain, armed)


# -- bare-handed to floor 4 ----------------------------------------------------


@pytest.mark.parametrize("pack", ["simulacra", "hardpan"])
def test_every_floor_has_a_weapon_to_find(pack):
    """"[attack: ...] bare-handed" on every floor from 1 to 4."""
    theme = Theme.load(pack)
    for ws in (1, 42, PLAYTEST_WORLD):
        for depth in range(1, 9):
            floor = looted(theme, ws, depth)
            assert any(i.damage > 0 for r in floor.rooms.values() for i in r.items), (ws, depth)


def test_the_weapon_lies_where_the_vault_should_have_been():
    floor = looted(Theme.load("hardpan"), PLAYTEST_WORLD, 1)
    assert not any(r.kind is RoomKind.VAULT for r in floor.rooms.values())
    shrine = next(r for r in floor.rooms.values() if r.kind is RoomKind.SHRINE)
    assert any(i.damage > 0 for i in shrine.items)


def test_a_floor_with_a_vault_gets_no_second_weapon(theme):
    def weapons(floor):
        return sum(i.damage > 0 for r in floor.rooms.values() for i in r.items)
    for ws in range(20):
        assert weapons(looted(theme, ws, 6)) == weapons(generate_floor(6, theme, floor_rng(ws, 6)))


# -- taking what a search found --------------------------------------------------


def test_taking_what_a_search_found_says_it_is_part_of_the_room(game):
    """"The brass scales hang suspended..." then "There is no scales here." """
    engine, state, store = game
    node = f"room:{state.room_id}"
    store.set_node_data(node, {**store.node_data(node), "found": {"scales": 1}})
    events = list(engine.turn("take scales"))
    assert any(isinstance(e, Notice) and "part of the room" in e.text for e in events)
    assert not any("There is no" in t for t in texts(events))


# -- narration is not dialogue ---------------------------------------------------


def test_what_an_npc_does_is_narrated_not_spoken(archivist):
    """"the Widow: The Widow takes it." """
    engine, state, store = archivist
    dealings.bump(store, "npc:archivist", dealings.ATTACK_STEP)
    state.player.inventory.append(Item(id="x", name="a jar of clean water", heal=6))

    for command, line in [("give water to archivist", "will not take it"),
                          ("follow archivist", "stay where they are")]:
        events = list(engine.turn(command))
        spoken = "".join(e.text for e in events if isinstance(e, ProseDelta))
        assert "They" not in spoken, command
        assert any(isinstance(e, Line) and line in e.text for e in events), command


# -- the 2b's names --------------------------------------------------------------


@pytest.mark.parametrize("name", ["{The Start}", "{The Hallway}", "{Nothingness}"])
def test_the_braced_stock_names_are_refused(name):
    theme = Theme.load("hardpan")
    floor = generate_floor(4, theme, random.Random(3))
    before = {r.id: r.name for r in floor.rooms.values()}
    rooms = [{"id": r.id, "name": name, "concept": "A low room."}
             for r in floor.rooms.values() if r.kind is not RoomKind.CORRIDOR]
    apply_floor_plan(floor, {"theme_name": "The Tally Board", "goal": "", "motifs": [],
                             "rooms": rooms}, theme=theme)
    assert all(r.name == before[r.id] for r in floor.rooms.values())


def test_braces_come_off_a_name_worth_keeping():
    theme = Theme.load("hardpan")
    floor = generate_floor(4, theme, random.Random(3))
    rid = next(r.id for r in floor.rooms.values() if r.kind is RoomKind.SHRINE)
    apply_floor_plan(floor, {"theme_name": "{The Weight of Stone}", "goal": "", "motifs": [],
                             "rooms": [{"id": rid, "name": "{The Tally Board}",
                                        "concept": "A low room."}]}, theme=theme)
    assert floor.theme_name == "The Weight of Stone"
    assert floor.rooms[rid].name == "The Tally Board"


# -- "is here." under every greeting ------------------------------------------------


def test_a_greeting_does_not_announce_who_is_already_here(archivist):
    engine, _, _ = archivist
    assert not any(isinstance(e, NpcPresent) for e in engine.turn("talk to archivist"))


def test_a_remembered_run_still_announces_itself(archivist):
    engine, state, store = archivist
    store.remember(state.run_id - 1, "A delver was killed by an offcut on floor 1.",
                   kind="death", subjects=["npc:archivist"], embedding=[0.0] * 768)
    store.commit()
    # Not "...the delvers who died": `who` is a `self` keyword, and that route
    # never touches past runs.
    present = [e for e in engine.turn("ask archivist about the delvers before me")
               if isinstance(e, NpcPresent)]
    assert present and present[0].remembers


# -- drink ---------------------------------------------------------------------------


def test_one_kind_of_thing_is_no_choice(game):
    engine, state, _ = game
    state.player.hp = 5
    state.player.inventory[:] = [Item(id="a", name="a canteen, still heavy", heal=6),
                                 Item(id="b", name="a canteen, still heavy", heal=7)]
    assert any("You use a canteen, still heavy" in t for t in texts(engine.turn("drink")))


def test_a_real_choice_is_listed_so_it_can_be_read(game):
    """"Use what? You have a tin of peaches, a canteen, still heavy, a tin of..." """
    engine, state, _ = game
    state.player.inventory[:] = [
        Item(id="a", name="a canteen, still heavy", heal=6),
        Item(id="b", name="a tin of peaches", heal=4),
        Item(id="c", name="a canteen, still heavy", heal=7),
        Item(id="d", name="a tin of peaches", heal=5),
    ]
    notices = [e.text for e in engine.turn("drink") if isinstance(e, Notice)]
    assert notices == ["Use what? You have a canteen, still heavy (×2); a tin of peaches (×2)."]


# -- floor 1 in the transcript ---------------------------------------------------------


def test_the_transcript_names_the_first_floor():
    buf = io.StringIO()
    renderer = ReplRenderer(Console(file=buf, width=100, highlight=False, color_system=None))
    renderer.handle(FloorNamed(depth=1, theme_name="The Weight of Stone",
                               goal="Clear the rubble before it crushes."))
    out = buf.getvalue()
    assert "Floor 1. The Weight of Stone" in out
    assert "Clear the rubble before it crushes." in out


# -- canon in the NPC's own voice ------------------------------------------------------


@pytest.mark.parametrize("line,spoken", [
    ("Keeps a plate warm for a shift that ended before the town emptied.",
     "I keep a plate warm for a shift that ended before the town emptied."),
    ("Has never been above the fourth level and does not want to go.",
     "I have never been above the fourth level and do not want to go."),
    ("Owes the Widow for a winter of suppers and intends to pay in silver.",
     "I owe the Widow for a winter of suppers and intend to pay in silver."),
    ("Does not believe the Assayer's scales are honest.",
     "I do not believe the Assayer's scales are honest."),
    ("Buys anything by weight and nothing by story.",
     "I buy anything by weight and nothing by story."),
    ("Will not go below the third floor, and will not say why.",
     "I will not go below the third floor, and will not say why."),
    ("I do not go below the third floor.", "I do not go below the third floor."),
    ("The ledger has been kept since before the numbering.",
     "The ledger has been kept since before the numbering."),
    ("Always keeps a lamp lit.", "Always keeps a lamp lit."),
])
def test_subjectless_canon_is_spoken_in_the_first_person(line, spoken):
    assert as_speaker(line) == spoken


@pytest.mark.parametrize("pack", ["simulacra", "hardpan"])
def test_every_authored_line_can_be_said(pack):
    for npc in Theme.load(pack).npcs:
        for line in npc.canon:
            assert as_speaker(line).startswith("I "), line


def test_the_npc_is_handed_its_canon_in_its_own_voice(archivist):
    engine, state, _ = archivist
    actor = next(a for a in state.room.actors if a.id == "npc:archivist")
    assert all(f.text.startswith("I ") for f in engine._router._self(state, actor, ""))


# -- an NPC reading its instructions back -----------------------------------------------


def test_an_npc_reciting_its_own_prompt_is_caught():
    """"You are a rogue assayer, unbound by conventional rules" -- twice."""
    assert recites_itself("You are a rogue assayer, unbound by conventional rules.", "the Assayer")
    assert not recites_itself("You are late, delver. Sit down.", "the Assayer")
    assert not recites_itself("I weigh what you bring up.", "the Assayer")


def test_it_says_nothing_rather_than_its_prompt(tmp_path):
    theme = Theme.load("hardpan")
    npc = next(n for n in theme.npcs if n.name == "the Assayer")
    client = FakeClient(script=["You are a rogue assayer, unbound by conventional rules, "
                                "whose scale measures truth in weight and value."])
    narrator = Narrator(client, theme, Store(tmp_path / "n.db"), Settings().narrator)
    brief = SimpleNamespace(canon=[], told=[], facts=[], label="", instruction="Answer them.")
    assert "".join(narrator.npc(npc, "hello", brief)) == Narrator.SILENT


def test_saying_nothing_is_narrated_not_spoken(tmp_path, theme):
    """The same bug as the gift line, one layer down: a reply the guards
    swallowed printed as "the Archivist: They say nothing." """
    settings = Settings()
    store = Store(tmp_path / "w.db")
    client = FakeClient(script=["What is true of you: you keep the ledger."])
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator), client=client)
    list(engine.begin())
    state.room_id = next(r.id for r in state.floor.rooms.values()
                         if any(a.id == "npc:archivist" for a in r.actors))

    events = list(engine.turn("talk to archivist"))
    assert not any(isinstance(e, ProseDelta) for e in events)
    assert any(isinstance(e, Line) and e.text == Narrator.SILENT for e in events)
    store.close()
