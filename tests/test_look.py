"""`look at <target>` -- detail on a specific item or actor.

Added closing out the POC: `look` previously only ever re-described the whole
room, discarding any trailing target (`docs/tasks/poc-closeout-tasks.md` T3).
"""

from __future__ import annotations

import pytest
from conftest import FakeClient

from simulacra.config import Settings
from simulacra.engine.events import Damage, Notice, ProseDelta, Roll, RoomEntered
from simulacra.engine.loop import Engine
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.narrate.narrator import Narrator
from simulacra.world.model import Actor, Item

DETAIL = "Its rim is chipped, but whatever's inside still steams."


@pytest.fixture
def game(tmp_path, theme):
    store = Store(tmp_path / "look.db")
    settings = Settings()
    client = FakeClient(script=[DETAIL])
    narrator = Narrator(client, theme, store, settings.narrator)
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme, narrator=narrator, client=client)
    list(engine.begin())
    state.room.items.append(Item(id="ration", name="an unspoiled ration"))
    state.room.actors.append(Actor(id="offcut", name="an offcut", hp=10, max_hp=10))
    yield engine, state, client
    store.close()


def test_look_with_no_target_is_unchanged(game):
    engine, _, _ = game
    events = list(engine.turn("look"))
    assert any(isinstance(e, RoomEntered) for e in events)


def test_look_at_an_item_returns_detail_not_the_room(game):
    engine, _, _ = game
    events = list(engine.turn("look at the ration"))
    text = "".join(e.text for e in events if isinstance(e, ProseDelta))
    assert text == DETAIL
    assert not any(isinstance(e, RoomEntered) for e in events)


def test_look_at_an_actor_returns_detail(game):
    engine, _, _ = game
    events = list(engine.turn("examine offcut"))
    text = "".join(e.text for e in events if isinstance(e, ProseDelta))
    assert text == DETAIL


def test_look_at_something_absent_is_refused(game):
    engine, _, _ = game
    events = list(engine.turn("look at the unicorn"))
    assert any(isinstance(e, Notice) for e in events)


def test_repeated_look_at_is_served_from_cache(game):
    engine, _, client = game
    list(engine.turn("look at ration"))
    calls = client.stream_calls
    list(engine.turn("look at ration"))
    assert client.stream_calls == calls


def test_looking_at_a_hostile_never_provokes(game):
    """Free like the whole-room look -- eyeballing an enemy isn't an action."""
    engine, _, _ = game
    events = list(engine.turn("look at offcut"))
    assert not any(isinstance(e, (Roll, Damage)) for e in events)
