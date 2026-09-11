"""M15: search adds to the room, and the third playtest's small fixes.

From the run-3 review (docs/playtests/2026-09-11b.md), roadmap R1 and R2. Every
test names the line of the transcript or the finding it answers.

Helpers that M15 adds are imported inside the tests that use them, so on the
committed build each of these fails on its own rather than the file failing to
import.
"""

from __future__ import annotations

import io
import re

import pytest
from rich.console import Console

from simulacra.config import Settings
from simulacra.engine.events import Line, Notice, ProseDelta, RoomEntered
from simulacra.engine.loop import Engine
from simulacra.engine.parser import parse
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.narrate.narrator import Narrator
from simulacra.ui.repl import ReplRenderer
from simulacra.world import discovery
from simulacra.world.model import Actor, Item

from conftest import FakeClient

ARCHIVIST = "npc:archivist"
# A find that names what it was asked about: `{Target}` becomes the fixture.
NAMED = "{Target}, its edge worn smooth by hands that are gone."
# One that names nothing, like 19 of the playtest's 29.
UNNAMED = "It is cold here, and quiet, and it has been for a long while."


class Asked(FakeClient):
    """Records each prompt. `{Target}` in a scripted reply becomes whatever the
    prompt's LOOKING AT line asked about, so a find can name its fixture
    without the test knowing which fixture the seed picks."""

    def __init__(self, script=None):
        super().__init__(script=script)
        self.prompts: list[str] = []

    def stream(self, messages, policy, *, kind="stream"):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        self.calls.append(kind)
        self.stream_calls += 1
        text = self._next()
        if "LOOKING AT: " in prompt:
            target = prompt.split("LOOKING AT: ", 1)[1].split("\n", 1)[0]
            text = text.replace("{Target}", target[:1].upper() + target[1:])
        for i in range(0, len(text), 8):
            yield text[i : i + 8]

    def complete(self, messages, policy, *, kind="complete"):
        self.prompts.append(messages[-1]["content"])
        return super().complete(messages, policy, kind=kind)


@pytest.fixture
def game(tmp_path, theme, monkeypatch):
    """The entrance of a fresh world, fertile, with no cache to find first."""
    monkeypatch.setattr(discovery, "is_fertile", lambda *a: True)
    settings = Settings()
    store = Store(tmp_path / "w.db", world_seed=7)
    client = Asked(script=["A doorway, framed in something that was brick."])
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator),
                    client=client)
    list(engine.begin())
    state.room.cache.clear()
    client.prompts.clear()
    client.stream_calls = 0
    yield engine, state, store, client
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
                         if any(a.id == ARCHIVIST for a in r.actors))
    yield engine, state, store
    store.close()


def said(events) -> str:
    return "".join(e.text for e in events if isinstance(e, ProseDelta))


def lines(events) -> list[str]:
    return [e.text for e in events if isinstance(e, Line)]


def notices(events) -> list[str]:
    return [e.text for e in events if isinstance(e, Notice)]


# -- R1: search adds to the room --------------------------------------------


def test_a_search_is_shown_the_description_the_player_read(game):
    """Given only the room's name and concept, the model wrote a room."""
    engine, state, _, client = game
    client.script = [NAMED]
    list(engine.turn("search"))

    asked = [p for p in client.prompts if "LOOKING AT:" in p]
    assert asked, "the search never reached the model"
    assert "DESCRIPTION: A doorway, framed in something that was brick." in asked[0]


def test_a_find_that_never_names_its_fixture_is_refused(game):
    """Asked for the harness, it described the room's lever; asked for the tag
    board, the Threshold's door. Neither may become the find."""
    engine, state, store, client = game
    client.script = [UNNAMED]
    events = list(engine.turn("search"))

    assert store.canon(f"room:{state.room.id}", provenance="derived") == []
    assert UNNAMED not in said(events)
    assert client.stream_calls == 2, "one strict retry, then nothing"


def test_a_find_named_on_the_retry_is_kept(game):
    engine, state, store, client = game
    client.script = [UNNAMED, NAMED]
    events = list(engine.turn("search"))

    rows = store.canon(f"room:{state.room.id}", provenance="derived")
    assert len(rows) == 1
    assert rows[0]["text"] in said(events)
    assert UNNAMED not in said(events)


def test_look_lists_what_a_search_turned_up(game):
    """The run's `look` after a search showed the room exactly as before."""
    engine, state, store, client = game
    client.script = [NAMED]
    list(engine.turn("search"))
    names = store.node_data(f"room:{state.room.id}").get("found_names")
    assert names and len(names) == 1

    assert f"Noticed here: {names[0]}." in lines(engine.turn("look"))


def test_a_find_is_still_part_of_the_room_next_run(game):
    engine, state, store, client = game
    client.script = [NAMED]
    list(engine.turn("search"))
    here = state.room.id
    name = store.node_data(f"room:{here}")["found_names"][0]

    later = new_run(store, engine.theme, Settings(), seed=99)
    engine2 = Engine(later, store, Settings(), engine.theme,
                     narrator=engine.narrator, client=client)
    list(engine2.begin())
    later.room_id = here
    assert f"Noticed here: {name}." in lines(engine2.turn("look"))


def test_a_find_from_before_m15_is_listed_by_its_fixture(game):
    """Worlds already hold finds that kept only their words."""
    engine, state, store, _ = game
    room = state.room
    fixture = engine.theme.fixture_names(room.kind)[0]
    node = f"room:{room.id}"
    cid = store.add_canon(node, "Something found before names were kept.", "derived")
    store.set_node_data(node, {**store.node_data(node),
                               "found": {w: cid for w in discovery.words(fixture)}})
    store.commit()

    assert f"Noticed here: {fixture}." in lines(engine.turn("look"))


def test_taking_what_a_find_described_is_part_of_the_room(game):
    """`take paper` after a find about a notice: "There is no paper here.\""""
    engine, _, _, client = game
    client.script = ["{Target}, and under it a sheet of paper gone soft with damp."]
    list(engine.turn("search"))

    assert any("part of the room" in n for n in notices(engine.turn("take paper")))


@pytest.mark.parametrize("text, target, named", [
    ("The tag board is rusted through at every hook.", "the tag board", True),
    ("The iron frame of the tagboard hangs from a rusted hinge.", "the tag board", True),
    ("A fragment of bone, colder than the floor.", "bones", True),
    ("Envelopes, every one of them opened and empty.", "pay envelopes", True),
    ("The timbers lean against each other like tired men.", "timbering", True),
    ("The straps on its side are worn smooth by wet iron.", "a strapped chest", True),
    ("The vertebrae stand rigid like old clockwork gears.", "bones", False),
    ("You watch the ribcage flex beneath its own weight.", "bones", False),
    ("Your eyes drift down over a rusted lever wedged in grease.", "harness", False),
])
def test_a_find_names_its_fixture_or_it_does_not(text, target, named):
    from simulacra.narrate import narrator
    assert narrator.names_its_find(text, target) is named


# -- R2: the small fixes ----------------------------------------------------


@pytest.mark.parametrize("text", ["greet powder monkey", "greet widow", "hello",
                                  "hi archivist", "hail the archivist"])
def test_a_greeting_is_talk_without_asking_the_model(text):
    """"greet Powder Monkey" went to the model fallback and came back a give."""
    intent = parse(text)
    assert intent is not None and intent.verb == "talk"


def test_the_rest_of_a_name_is_not_a_topic():
    from simulacra.engine import loop
    monkey = Actor(id="npc:monkey", name="the Powder Monkey", hp=1, max_hp=1, hostile=False)
    assert loop._past_the_name("monkey", monkey) == ""
    assert loop._past_the_name("monkey what are you carrying", monkey) == "what are you carrying"
    assert loop._past_the_name("widow", monkey) == "widow"


def test_talking_to_a_two_word_name_asks_nothing(archivist):
    """`talk to Powder Monkey` arrived as addressee "powder", topic "monkey"."""
    engine, state, _ = archivist
    npc = next(a for a in state.room.actors if a.id == ARCHIVIST)
    npc.name = "the Powder Monkey"

    list(engine.turn("talk to powder monkey"))
    assert engine._conversations[ARCHIVIST].asked == {""}


def test_telling_a_two_word_name_keeps_the_claim_whole(archivist):
    engine, state, store = archivist
    npc = next(a for a in state.room.actors if a.id == ARCHIVIST)
    npc.name = "the Powder Monkey"

    list(engine.turn("tell powder monkey the widow is dead"))
    told = [r["text"] for r in store.canon(ARCHIVIST, provenance="told")]
    assert told == ["widow is dead"]


def test_the_pack_is_listed_by_kind(game):
    """Eleven lines, four of them the same tin of peaches."""
    engine, state, _, _ = game
    state.player.inventory[:] = [
        Item(id="p1", name="a tin of peaches", heal=4),
        Item(id="p2", name="a tin of peaches", heal=4),
        Item(id="p3", name="a tin of peaches", heal=5),
        Item(id="h1", name="a hogleg", damage=3),
    ]
    assert lines(engine.turn("inventory")) == [
        "You carry:",
        "  a tin of peaches ×2 (heals 4)",
        "  a tin of peaches (heals 5)",
        "  a hogleg (damage 3)",
    ]


def test_the_exits_say_which_ways_are_new(game):
    """Floor 6: 18 of 73 commands were "There are no stairs here.\""""
    engine, state, _, _ = game
    first = next(e for e in engine.turn("look") if isinstance(e, RoomEntered))
    assert set(first.unexplored) == set(first.exits)

    way = next(iter(state.room.exits))
    list(engine.turn(way.value))
    back = next(e for e in engine.turn("look") if isinstance(e, RoomEntered))
    assert way.opposite.value in back.exits
    assert way.opposite.value not in back.unexplored


def test_the_repl_marks_unexplored_exits():
    out = io.StringIO()
    repl = ReplRenderer(Console(file=out, width=100, highlight=False, markup=False))
    repl.handle(RoomEntered(room_id="a", name="a drift", exits=("west", "north", "south"),
                            first_visit=False, unexplored=("west",)))
    assert "exits: N S W  (unexplored: W)" in out.getvalue()


@pytest.mark.parametrize("text, kept", [
    # The run's own, cut off mid-sentence and counting.
    ("The delver falls to floor seven after thirty-eight turns because he refuses to move as", ""),
    ("The delver fell on floor 7.", ""),
    ('"The shaft keeps what it takes." And more after.', "The shaft keeps what it takes."),
    ("No one comes down for the delver.", "No one comes down for the delver."),
])
def test_an_epitaph_is_one_whole_sentence_with_no_count(text, kept):
    from simulacra.narrate import narrator
    assert narrator.one_sentence(text) == kept


def test_the_epitaph_is_not_handed_numbers(theme, tmp_path):
    """Handed "308 turns", the 2b wrote "thirty-eight turns"."""
    client = Asked(script=["The shaft keeps what it takes."])
    store = Store(tmp_path / "e.db")
    narrator = Narrator(client, theme, store, Settings().narrator)

    assert narrator.epitaph("a claim rat", 7, 308) == "The shaft keeps what it takes."
    assert not re.search(r"\d", client.prompts[-1])
    store.close()
