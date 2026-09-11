"""M11.1: the second 2026-09-10 playtest.

The first one played in `hardpan`. The worst thing it found is at the top: a
verb the parser did not know walked the player through an exit they never named,
into the room that killed them.
"""

from __future__ import annotations

import builtins

import pytest

from simulacra.config import Settings
from simulacra.engine.events import Notice, NpcPresent
from simulacra.engine.loop import Engine
from simulacra.engine.parser import infer, parse
from simulacra.engine.routes import Brief
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.narrate.narrator import Narrator
from simulacra.world.model import Item, RoomKind
from simulacra.world.theme import Theme

from conftest import FakeClient

ARCHIVIST = "npc:archivist"


@pytest.fixture
def game(tmp_path, theme):
    settings = Settings()
    store = Store(tmp_path / "w.db", world_seed=11)
    client = FakeClient(script=["A low room, and the smell of old wicks."])
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator),
                    client=client)
    list(engine.begin())
    yield engine, state, store, client
    store.close()


# -- F1: the fallback may only look for a look, and move for a move ----------


@pytest.mark.parametrize("answer", [
    {"verb": "move", "target": "north"},
    {"verb": "move", "target": "west"},
])
def test_the_fallback_cannot_walk_you_through_an_exit_you_never_named(answer):
    """`dodge` came back as a move in the second playtest, into the room that
    killed the player."""
    client = FakeClient(structured_result=answer)
    intent = infer("wiggle out of the way", "a stope. exits: north, west", client,
                   Settings().intent)
    assert intent.verb == "improvise"


def test_a_move_the_player_typed_is_still_a_move():
    client = FakeClient(structured_result={"verb": "move", "target": "north"})
    intent = infer("run north", "a stope. exits: north", client, Settings().intent)
    assert (intent.verb, intent.target) == ("move", "north")


@pytest.mark.parametrize("text", ["wiggle", "water", "open tin of peaches"])
def test_the_fallback_cannot_answer_an_action_with_the_room(text):
    """`jump`, `slam`, `water`, `eat tin of peaches` all came back as the room
    description -- the fallback answered `look`, and M11's guard kept the verb."""
    client = FakeClient(structured_result={"verb": "look", "target": ""})
    assert infer(text, "a stope", client, Settings().intent).verb == "improvise"


def test_a_look_the_player_typed_is_still_a_look():
    client = FakeClient(structured_result={"verb": "look", "target": ""})
    assert infer("glance around", "a stope", client, Settings().intent).verb == "look"


def test_an_unknown_verb_never_moves_the_player(game):
    engine, state, _, client = game
    client.structured_result = {"verb": "move", "target": next(iter(state.room.exits)).value}
    before = state.room_id
    list(engine.turn("wiggle"))
    assert state.room_id == before


# -- F2: the verbs the second playtest reached for ---------------------------


@pytest.mark.parametrize("text", ["jump", "dodge", "dodge the blow", "climb the ladder",
                                  "leap across", "duck", "sneak past"])
def test_physical_actions_go_straight_to_the_judge(text):
    intent = parse(text)
    assert intent is not None and intent.verb == "improvise"
    assert intent.target == intent.raw


@pytest.mark.parametrize("text,verb", [
    ("smash", "attack"), ("slam the dry hand", "attack"), ("bash", "attack"),
    ("eat tin of peaches", "use"), ("consume ration", "use"),
])
def test_attack_and_use_words(text, verb):
    assert parse(text).verb == verb


@pytest.mark.parametrize("text", ["search room", "search the room", "search here",
                                  "search around", "search this room", "search the whole room"])
def test_searching_the_room_is_a_plain_search(text):
    intent = parse(text)
    assert (intent.verb, intent.target) == ("search", "")


def test_drink_uses_the_only_thing_you_could_mean(game):
    engine, state, _, _ = game
    state.player.hp = 5
    state.player.inventory.append(Item(id="i:c", name="a canteen, still heavy", heal=6))
    list(engine.turn("drink"))
    assert state.player.hp == 11 and not state.player.inventory


def test_drink_water_finds_the_canteen(game):
    """Hardpan's drinks are not called water."""
    engine, state, _, _ = game
    state.player.hp = 5
    state.player.inventory.append(Item(id="i:c", name="a canteen, still heavy", heal=6))
    list(engine.turn("drink water"))
    assert state.player.hp == 11


def test_use_does_not_guess_at_a_named_thing(game):
    """The fallback is for drinking and eating. `use rope` with a canteen in the
    pack is not a request for the canteen."""
    engine, state, _, _ = game
    state.player.hp = 5
    state.player.inventory.append(Item(id="i:c", name="a canteen, still heavy", heal=6))
    events = list(engine.turn("use rope"))
    assert state.player.hp == 5
    assert any(isinstance(e, Notice) for e in events)


def test_with_two_things_it_asks_and_names_them(game):
    engine, state, _, _ = game
    state.player.inventory += [Item(id="i:c", name="a canteen", heal=6),
                               Item(id="i:p", name="a tin of peaches", heal=4)]
    events = list(engine.turn("drink"))
    note = next(e.text for e in events if isinstance(e, Notice))
    assert "canteen" in note and "peaches" in note


def test_down_where_there_are_no_stairs_says_so(game):
    engine, state, _, _ = game
    assert state.room.kind is not RoomKind.DESCENT
    events = list(engine.turn("d"))
    assert any(isinstance(e, Notice) and "no stairs" in e.text for e in events)


def test_once_found_it_says_where_the_stairs_were(game):
    """Twenty turns of the second playtest went on pressing `d`."""
    engine, state, _, _ = game
    stairs = next(r for r in state.floor.rooms.values() if r.kind is RoomKind.DESCENT)
    stairs.visited = True
    events = list(engine.turn("d"))
    assert any(isinstance(e, Notice) and stairs.name in e.text for e in events)


# -- F3: three bugs the transcript showed ------------------------------------


def test_remembers_you_means_a_previous_run(game):
    """The Assayer "remembered" the delver after one question about peaches."""
    engine, state, _, _ = game
    state.room_id = next(r.id for r in state.floor.rooms.values()
                         if any(a.id == ARCHIVIST for a in r.actors))
    state.room.items.append(Item(id="i:p", name="a tin of peaches", heal=4))
    list(engine.turn("ask archivist about the peaches"))

    present = [e for e in engine.turn("look") if isinstance(e, NpcPresent)]
    assert present and not any(e.remembers for e in present)


def test_the_npc_prompt_reads_as_a_sentence(tmp_path):
    """"You are the Assayer. weighs what you bring up..." was echoed back as
    "You weigh what you bring up and tell you what it is worth."."""
    theme = Theme.load("hardpan")
    store = Store(tmp_path / "w.db")
    prompts = []

    class Recorder(FakeClient):
        def stream(self, messages, policy, *, kind="stream"):
            prompts.append(messages)
            yield from super().stream(messages, policy, kind=kind)

    narrator = Narrator(Recorder(script=["That don't weigh."]), theme, store,
                        Settings().narrator)
    assayer = next(n for n in theme.npcs if n.anchor == "npc:assayer")
    list(narrator.npc(assayer, "hello", Brief(route="self", instruction="Answer.")))

    system = prompts[0][0]["content"]
    assert system.startswith("You are the Assayer, who weighs what you bring up")
    store.close()


def test_each_world_gets_its_own_transcript(tmp_path, monkeypatch):
    """Run numbers restart with every new world, and three worlds' runs were
    appended into one file."""
    from simulacra.__main__ import main

    lines = iter(["quit", "quit"])
    monkeypatch.setattr(builtins, "input", lambda *_: next(lines))
    db = str(tmp_path / "w.db")
    assert main(["--offline", "--db", db, "--seed", "3"]) == 0
    assert main(["--offline", "--db", db, "--new-world", "--seed", "4"]) == 0

    names = sorted(p.name for p in (tmp_path / "transcripts").glob("*.txt"))
    assert names == ["w-3-run1.txt", "w-4-run1.txt"]


@pytest.mark.parametrize("text,verb", [("/exit", "quit"), ("/look", "look"),
                                       ("/i", "inventory"), ("!quit", "quit")])
def test_a_leading_slash_is_a_reflex_not_a_command(text, verb):
    """`/exit` reached the fallback and came back as the room description --
    found by the live check for this milestone, after the tests were green."""
    assert parse(text).verb == verb


def test_the_fallback_cannot_enter_the_room_you_are_in():
    client = FakeClient(structured_result={"verb": "enter", "target": "headframe"})
    intent = infer("wander off", "the Headframe. exits: south", client, Settings().intent)
    assert intent.verb == "improvise"


def test_an_enter_the_player_typed_is_still_an_enter():
    client = FakeClient(structured_result={"verb": "enter", "target": "stope"})
    intent = infer("go into the stope", "a drift. exits: north", client, Settings().intent)
    assert (intent.verb, intent.target) == ("enter", "stope")
