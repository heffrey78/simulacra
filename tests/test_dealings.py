"""M9: things change hands, and NPCs hold opinions about you.

The rule under test throughout is that the model proposes and code disposes: it
returns a decision, and nothing moves until code has checked the decision against
what is actually true. A model that can invent an object can conjure one
permanently into a world that persists.
"""

from __future__ import annotations

import pytest

from simulacra.config import Settings
from simulacra.engine import dealings
from simulacra.engine.events import Line, Notice, ProseDelta
from simulacra.engine.loop import Engine
from simulacra.engine.state import ensure_npcs, new_run
from simulacra.memory.store import Store
from simulacra.narrate.narrator import Narrator
from simulacra.world.model import Item
from simulacra.world.theme import Theme

from conftest import FakeClient

ARCHIVIST = "npc:archivist"
BLADE = Item(id="i:blade", name="a stuttering blade", damage=4)
WATER = Item(id="i:water", name="a jar of clean water", heal=6)


class Deciding(FakeClient):
    """Returns a fixed decision, and counts what it was asked."""

    def __init__(self, decision=None, **kw):
        super().__init__(script=["..."], **kw)
        self.decision = decision
        self.decisions = 0

    def structured(self, messages, schema, policy, *, kind="structured", retries=1):
        self.decisions += 1
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision


@pytest.fixture
def facing(tmp_path, theme):
    """The player standing with the Archivist, carrying a blade."""
    settings = Settings()
    store = Store(tmp_path / "w.db")
    client = Deciding()
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator),
                    client=client)
    list(engine.begin())
    host = next(r for r in state.floor.rooms.values()
                if any(a.id == ARCHIVIST for a in r.actors))
    state.room_id = host.id
    state.player.inventory.append(BLADE)
    client.decisions = 0
    yield engine, state, store, client
    store.close()


def said(events) -> str:
    return "".join(e.text for e in events if isinstance(e, ProseDelta))


# -- A1: the vocabulary refuses rather than inventing -----------------------


@pytest.mark.parametrize("bad", [
    None,
    {"act": "seize", "object": "a stuttering blade", "reason": "mine now"},
    {"act": "give", "object": "a crown of teeth", "reason": "here"},  # never existed
    RuntimeError("model went away"),
])
def test_a_bad_decision_can_never_hand_anything_over(facing, bad):
    engine, state, store, client = facing
    dealings.take(store, ARCHIVIST, WATER)
    client.decision = bad

    before = list(state.player.inventory)
    list(engine.turn("ask archivist for the water"))

    assert state.player.inventory == before
    assert len(dealings.holdings(store, ARCHIVIST)) == 1


def test_an_invented_object_is_not_a_match():
    rows = [{"id": "i:water", "name": "a jar of clean water"}]
    assert dealings.match("a crown of teeth", rows) is None
    assert dealings.match("", rows) is None
    assert dealings.match("clean water", rows) == rows[0]


# -- A2: giving ------------------------------------------------------------


def test_giving_something_you_do_not_carry_costs_no_model_call(facing):
    engine, state, _, client = facing
    events = list(engine.turn("give the crown to the archivist"))
    assert any(isinstance(e, Notice) for e in events)
    assert client.decisions == 0


def test_an_accepted_gift_actually_moves(facing):
    engine, state, store, client = facing
    client.decision = {"act": "accept", "object": "a stuttering blade",
                       "reason": "It is entered."}

    events = list(engine.turn("give the blade to the archivist"))

    assert BLADE not in state.player.inventory
    assert [r["name"] for r in dealings.holdings(store, ARCHIVIST)] == [BLADE.name]
    assert "It is entered." in said(events)
    assert any(isinstance(e, Line) for e in events)


def test_a_gift_moves_disposition_by_exactly_one_step(facing):
    engine, state, store, client = facing
    client.decision = {"act": "accept", "object": "a stuttering blade", "reason": ""}
    before = dealings.disposition(store, ARCHIVIST)

    list(engine.turn("give the blade to the archivist"))
    assert dealings.disposition(store, ARCHIVIST) == before + dealings.GIFT_STEP


def test_a_refused_gift_moves_nothing(facing):
    engine, state, store, client = facing
    client.decision = {"act": "refuse", "object": "", "reason": "Not in the ledger."}

    list(engine.turn("give the blade to the archivist"))
    assert BLADE in state.player.inventory
    assert dealings.holdings(store, ARCHIVIST) == []
    assert dealings.disposition(store, ARCHIVIST) == 0


def test_a_wary_npc_refuses_a_gift_for_free(facing):
    """M8's rule: a refusal code can decide is code's job."""
    engine, state, store, client = facing
    dealings.bump(store, ARCHIVIST, -5)

    list(engine.turn("give the blade to the archivist"))
    assert client.decisions == 0
    assert BLADE in state.player.inventory


# -- A3: asking for something ----------------------------------------------


def test_a_warm_npc_hands_it_over(facing):
    engine, state, store, client = facing
    dealings.take(store, ARCHIVIST, WATER)
    dealings.bump(store, ARCHIVIST, 2)
    client.decision = {"act": "give", "object": "a jar of clean water", "reason": "Take it."}

    list(engine.turn("ask archivist for the water"))

    assert [i.name for i in state.player.inventory if i.id == "i:water"] == [WATER.name]
    assert dealings.holdings(store, ARCHIVIST) == []


def test_asking_an_npc_holding_nothing_costs_no_model_call(facing):
    engine, state, _, client = facing
    list(engine.turn("ask archivist for the water"))
    assert client.decisions == 0


def test_a_wary_npc_refuses_a_request_for_free(facing):
    engine, state, store, client = facing
    dealings.take(store, ARCHIVIST, WATER)
    dealings.bump(store, ARCHIVIST, -5)

    list(engine.turn("ask archivist for the water"))
    assert client.decisions == 0
    assert len(dealings.holdings(store, ARCHIVIST)) == 1


def test_an_item_handed_over_keeps_working(facing):
    """A jar of water that no longer heals is a bug the player finds later."""
    engine, state, store, client = facing
    dealings.take(store, ARCHIVIST, WATER)
    dealings.bump(store, ARCHIVIST, 2)
    client.decision = {"act": "give", "object": "a jar of clean water", "reason": ""}

    list(engine.turn("ask archivist for the water"))
    got = next(i for i in state.player.inventory if i.id == "i:water")
    assert got.heal == WATER.heal


# -- A4: disposition -------------------------------------------------------


def test_the_bands(tmp_path, theme):
    store = Store(tmp_path / "w.db")
    ensure_npcs(store, theme)

    assert not dealings.is_wary(store, ARCHIVIST) and not dealings.is_warm(store, ARCHIVIST)
    dealings.bump(store, ARCHIVIST, dealings.WARM)
    assert dealings.is_warm(store, ARCHIVIST)
    dealings.bump(store, ARCHIVIST, -10)
    assert dealings.is_wary(store, ARCHIVIST)
    store.close()


def test_disposition_is_capped(tmp_path, theme):
    """Without a cap the player empties their pack into one NPC and buys every
    band, which makes the bands meaningless."""
    store = Store(tmp_path / "w.db")
    ensure_npcs(store, theme)
    for _ in range(20):
        dealings.bump(store, ARCHIVIST, dealings.GIFT_STEP)
    assert dealings.disposition(store, ARCHIVIST) == dealings.DISPOSITION_CAP
    store.close()


def test_disposition_outlives_the_run(tmp_path, theme):
    store = Store(tmp_path / "w.db")
    settings = Settings()
    new_run(store, theme, settings, seed=1)
    dealings.bump(store, ARCHIVIST, 2)

    new_run(store, theme, settings, seed=2)
    assert dealings.disposition(store, ARCHIVIST) == 2
    store.close()


def test_forget_resets_disposition(tmp_path, theme):
    store = Store(tmp_path / "w.db")
    ensure_npcs(store, theme)
    dealings.bump(store, ARCHIVIST, 3)
    store.forget(ARCHIVIST)
    assert dealings.disposition(store, ARCHIVIST) == 0
    store.close()


def test_a_gift_buys_a_route_not_an_item(facing):
    """systems.md's phrasing, and the reason this is more interesting than a
    shop: a stranger gets the official line, someone who has given them
    something gets the history the canonist worked out."""
    engine, state, store, _ = facing
    store.add_canon(ARCHIVIST, "Counts the doors as well, lately.", "derived")
    store.commit()
    actor = next(a for a in state.room.actors if a.id == ARCHIVIST)

    cold = [f.text for f in engine._router._self(state, actor, "yourself")]
    assert "Counts the doors as well, lately." not in cold

    dealings.bump(store, ARCHIVIST, dealings.WARM)
    warm = [f.text for f in engine._router._self(state, actor, "yourself")]
    assert "Counts the doors as well, lately." in warm


def test_authored_canon_is_never_gated(facing):
    """M7's gate is "a backstory that is not a death". It has to hold for a
    stranger, or M9 has quietly taken a milestone back."""
    engine, state, store, _ = facing
    actor = next(a for a in state.room.actors if a.id == ARCHIVIST)
    npc = next(n for n in engine.theme.npcs if n.anchor == ARCHIVIST)

    texts = [f.text for f in engine._router._self(state, actor, "yourself")]
    from simulacra.engine.routes import as_speaker
    assert as_speaker(npc.canon[0]) in texts


# -- A5: following ---------------------------------------------------------


def test_a_neutral_npc_declines_to_follow_for_free(facing):
    engine, state, _, client = facing
    list(engine.turn("follow me"))
    assert client.decisions == 0
    assert engine._following == set()


def test_a_warm_npc_follows_and_its_position_is_written_down(facing):
    """M7 claimed inverting placement would make movement a state update rather
    than a floorgen rewrite. This is the test that cashes it."""
    engine, state, store, client = facing
    dealings.bump(store, ARCHIVIST, dealings.WARM)
    list(engine.turn("follow me"))
    assert ARCHIVIST in engine._following

    direction = next(iter(state.room.exits))
    dest = state.room.exits[direction]
    list(engine.turn(direction.value))

    assert any(a.id == ARCHIVIST for a in state.floor.rooms[dest].actors)
    assert store.node_data(ARCHIVIST)["room_id"] == dest
    assert client.decisions == 0, "walking is not a decision for the model"


def test_a_led_npc_is_where_you_left_it_next_run(facing):
    engine, state, store, _ = facing
    dealings.bump(store, ARCHIVIST, dealings.WARM)
    list(engine.turn("follow me"))
    direction = next(iter(state.room.exits))
    dest = state.room.exits[direction]
    list(engine.turn(direction.value))

    later = new_run(store, engine.theme, Settings(), seed=9)
    assert any(a.id == ARCHIVIST for a in later.floor.rooms[dest].actors)


def test_following_toggles_off(facing):
    engine, state, store, _ = facing
    dealings.bump(store, ARCHIVIST, dealings.WARM)
    list(engine.turn("follow me"))
    list(engine.turn("follow me"))
    assert engine._following == set()


def test_nobody_follows_you_into_a_new_run(facing):
    """Their position persists; the walking does not."""
    engine, state, store, _ = facing
    dealings.bump(store, ARCHIVIST, dealings.WARM)
    list(engine.turn("follow me"))

    fresh = Engine(new_run(store, engine.theme, Settings(), seed=3), store,
                   Settings(), engine.theme)
    assert fresh._following == set()


# -- A6: the trade route ---------------------------------------------------


def test_the_trade_route_resolves_to_what_they_carry(facing):
    engine, state, store, _ = facing
    dealings.take(store, ARCHIVIST, WATER)
    actor = next(a for a in state.room.actors if a.id == ARCHIVIST)

    facts = engine._router._trade(state, actor, "what are you carrying")
    assert any(WATER.name in f.text for f in facts)


def test_a_wary_npc_shows_you_nothing(facing):
    engine, state, store, _ = facing
    dealings.take(store, ARCHIVIST, WATER)
    dealings.bump(store, ARCHIVIST, -5)
    actor = next(a for a in state.room.actors if a.id == ARCHIVIST)

    assert engine._router._trade(state, actor, "what are you carrying") == []


def test_carrying_routes_to_trade(facing):
    engine, state, _, _ = facing
    actor = next(a for a in state.room.actors if a.id == ARCHIVIST)
    assert engine._router.classify("what are you carrying", actor)[0] == "trade"


# -- the wipe --------------------------------------------------------------


def test_talking_to_an_npc_does_not_erase_what_it_is(facing):
    """`_talk` touches the node to update `last_run`, and `data=None` used to
    mean `{}` -- so every conversation turn silently erased the NPC's
    disposition, inventory, home room and canon gate. Invisible until M7 put
    state on those nodes; found by M9 when a gift stopped counting."""
    engine, state, store, client = facing
    dealings.take(store, ARCHIVIST, WATER)
    dealings.bump(store, ARCHIVIST, 2)
    before = store.node_data(ARCHIVIST)

    list(engine.turn("talk to archivist"))

    after = store.node_data(ARCHIVIST)
    assert after.get("disposition") == before["disposition"]
    assert after.get("inventory") == before["inventory"]
    assert after.get("room_id") == before["room_id"]


def test_touching_a_node_keeps_its_blob(tmp_path, theme):
    store = Store(tmp_path / "w.db")
    store.upsert_node("npc:x", "npc", "X", {"disposition": 3})
    store.upsert_node("npc:x", "npc", "X", run_id=7)  # a touch, not a write
    assert store.node_data("npc:x") == {"disposition": 3}

    store.upsert_node("npc:x", "npc", "X", {})  # explicit clear still clears
    assert store.node_data("npc:x") == {}
    store.close()
