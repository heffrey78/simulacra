"""M3 engine wiring: consequence, escalation, death."""

from __future__ import annotations

import pytest

from simulacra.config import Settings
from simulacra.engine.events import Damage, Line, Notice, Roll, RunEnded, StatusChanged
from simulacra.engine.loop import Engine
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.narrate.narrator import Narrator
from simulacra.world.model import Actor, Item

from conftest import FakeClient


def mob(hp=8, attack=2, defense=8, name="a smudged figure"):
    return Actor(id="m1", name=name, hp=hp, max_hp=hp, attack=attack, defense=defense)


@pytest.fixture
def game(tmp_path, theme):
    store = Store(tmp_path / "m3.db")
    settings = Settings()
    state = new_run(store, theme, settings, seed=42)
    engine = Engine(state, store, settings, theme)
    yield engine, state, store
    store.close()


def drain(engine, text):
    return list(engine.turn(text))


def of(events, cls):
    return [e for e in events if isinstance(e, cls)]


# -- combat ----------------------------------------------------------------


def test_attacking_rolls_and_can_damage(game):
    engine, state, _ = game
    list(engine.begin())
    state.room.actors.append(mob())
    events = drain(engine, "attack figure")
    assert of(events, Roll)


def test_attacking_nothing_is_refused_and_costs_no_monster_turn(game):
    engine, state, _ = game
    list(engine.begin())
    events = drain(engine, "attack")
    assert of(events, Notice)
    assert not of(events, Roll)


def test_monsters_act_after_a_resolved_action(game):
    engine, state, _ = game
    list(engine.begin())
    state.room.actors.append(mob(hp=200, defense=99))  # unkillable, always retaliates
    events = drain(engine, "attack figure")
    # Two Roll labels: the player's attack, then the monster's.
    labels = [e.label for e in of(events, Roll)]
    assert "attack" in labels
    assert any(lbl.startswith("a smudged figure") for lbl in labels)


def test_a_typo_does_not_provoke_the_monsters(game):
    """M1 assumed every input was a turn for the monsters. Being killed by a
    typo is bad, and stage 2 already makes unparsed input cost time."""
    engine, state, _ = game
    list(engine.begin())
    state.room.actors.append(mob(hp=200, defense=99))
    before = state.player.hp

    events = drain(engine, "xyzzy")
    assert not of(events, Roll)
    assert state.player.hp == before


def test_the_turn_counter_still_increments_on_a_typo(game):
    engine, state, _ = game
    list(engine.begin())
    before = state.turns
    drain(engine, "xyzzy")
    assert state.turns == before + 1


def test_fleeing_is_free(game):
    engine, state, _ = game
    list(engine.begin())
    state.room.actors.append(mob(hp=200, defense=99))
    direction = next(iter(state.room.exits)).value
    events = drain(engine, direction)
    # Moving resolves, so the monster in the room we LEFT does not follow.
    assert not any(e.label.startswith("a smudged") for e in of(events, Roll))


# -- items -----------------------------------------------------------------


def test_using_a_healing_item_restores_hp_and_consumes_it(game):
    engine, state, _ = game
    list(engine.begin())
    state.player.hp = 10
    state.player.inventory.append(Item(id="i", name="a jar of clean water", heal=6))

    events = drain(engine, "drink jar")
    assert state.player.hp == 16
    assert state.player.inventory == []
    assert of(events, StatusChanged)


def test_using_a_weapon_is_refused_politely(game):
    engine, state, _ = game
    list(engine.begin())
    state.player.inventory.append(Item(id="i", name="a stuttering blade", damage=4))
    assert of(drain(engine, "use blade"), Notice)


def test_using_something_you_lack_is_refused(game):
    engine, _, _ = game
    list(engine.begin())
    assert of(drain(engine, "drink wine"), Notice)


# -- escalation ------------------------------------------------------------


def test_stage_one_hits_cost_no_model_call(tmp_path, theme):
    """If stage 2 lands on the hot path, the zero-cost-turn target is gone."""
    store = Store(tmp_path / "hot.db")
    settings = Settings()
    client = FakeClient(structured_result={"verb": "look", "target": ""})
    state = new_run(store, theme, settings, seed=42)
    engine = Engine(state, store, settings, theme, client=client)

    list(engine.begin())
    client.calls.clear()
    for cmd in ("look", "inventory", "wait", next(iter(state.room.exits)).value):
        list(engine.turn(cmd))
    assert client.calls == [], f"stage 1 escalated: {client.calls}"


def test_unparsed_input_escalates_to_the_judge(tmp_path, theme):
    store = Store(tmp_path / "esc.db")
    settings = Settings()

    class Sequenced(FakeClient):
        def structured(self, messages, schema, policy, *, kind="structured", retries=1):
            self.calls.append(kind)
            if "verb" in str(schema):
                return {"verb": "improvise", "target": ""}
            return {"plausible": True, "difficulty": 5, "effect": "nothing",
                    "magnitude": 0, "reason": "You manage it, barely."}

    client = Sequenced()
    state = new_run(store, theme, settings, seed=42)
    engine = Engine(state, store, settings, theme, client=client)
    list(engine.begin())

    events = list(engine.turn("flood the room with water"))
    from simulacra.engine.events import Improvised
    assert of(events, Improvised)
    store.close()


# -- death -----------------------------------------------------------------


def test_death_ends_the_run_with_a_cause(game):
    engine, state, _ = game
    list(engine.begin())
    state.player.hp = 1
    state.room.actors.append(mob(hp=200, defense=99, attack=20))

    ended = None
    for _ in range(20):
        events = drain(engine, "attack figure")
        if of(events, RunEnded):
            ended = of(events, RunEnded)[0]
            break
    assert ended is not None, "the player never died"
    assert ended.cause == "a smudged figure"
    assert not state.player.alive


def test_death_is_recorded_in_the_graph(game):
    engine, state, store = game
    list(engine.begin())
    state.player.hp = 1
    state.room.actors.append(mob(hp=200, defense=99, attack=20))
    for _ in range(20):
        if of(drain(engine, "attack figure"), RunEnded):
            break

    run = f"run:{state.run_id}"
    assert store.neighbors(run, "DIED_IN"), "no DIED_IN edge"
    assert store.neighbors(run, "KILLED_BY"), "no KILLED_BY edge"


def test_epitaph_is_requested_on_death(tmp_path, theme):
    store = Store(tmp_path / "ep.db")
    settings = Settings()
    client = FakeClient(script=["Numbered, and then not."])
    narrator = Narrator(client, theme, store, settings.narrator)
    state = new_run(store, theme, settings, seed=42)
    engine = Engine(state, store, settings, theme, narrator=narrator, client=client)

    list(engine.begin())
    state.player.hp = 1
    state.room.actors.append(mob(hp=200, defense=99, attack=20))

    ended = None
    for _ in range(20):
        found = of(list(engine.turn("attack figure")), RunEnded)
        if found:
            ended = found[0]
            break
    assert ended and ended.epitaph == "Numbered, and then not."
    store.close()


def test_a_failing_epitaph_does_not_break_the_death(tmp_path, theme):
    store = Store(tmp_path / "ep2.db")
    settings = Settings()

    class Boom(FakeClient):
        def complete(self, messages, policy, *, kind="complete"):
            raise RuntimeError("model down")

    narrator = Narrator(Boom(), theme, store, settings.narrator)
    state = new_run(store, theme, settings, seed=42)
    engine = Engine(state, store, settings, theme, narrator=narrator)

    list(engine.begin())
    state.player.hp = 1
    state.room.actors.append(mob(hp=200, defense=99, attack=20))
    ended = None
    for _ in range(20):
        found = of(list(engine.turn("attack figure")), RunEnded)
        if found:
            ended = found[0]
            break
    assert ended and ended.epitaph == ""
    store.close()
