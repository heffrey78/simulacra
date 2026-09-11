"""M14: looting. Fights and searches pay off in things you can carry.

Tier 0 throughout, and every item from a theme pool through seeded dice -- the
model never names or invents loot. The balance side is in test_combat.py, where
the simulation now plays with drops and caches.
"""

from __future__ import annotations

import random

import pytest

from simulacra.config import Settings
from simulacra.engine import dealings, loot
from simulacra.engine.events import ItemTaken, Notice
from simulacra.engine.loop import Engine
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.narrate.narrator import Narrator
from simulacra.world.floorgen import floor_rng, generate_floor, make_item
from simulacra.world.model import Actor, Item
from simulacra.world.theme import Theme

from conftest import FakeClient
from test_judge import Rigged


@pytest.fixture
def game(tmp_path, theme):
    settings = Settings()
    store = Store(tmp_path / "w.db")
    state = new_run(store, theme, settings, seed=7)
    engine = Engine(state, store, settings, theme)
    list(engine.begin())
    yield engine, state, store
    store.close()


def texts(events) -> list[str]:
    return [getattr(e, "text", "") for e in events]


def mob(**kw) -> Actor:
    base = dict(id="m", name="an offcut", hp=1, max_hp=1, attack=0, defense=5,
                archetype="weak")
    base.update(kw)
    return Actor(**base)


def always_drop(monkeypatch):
    monkeypatch.setattr(loot, "DROP_CHANCE", {"weak": 1.0, "normal": 1.0, "strong": 1.0})


def shape(floor):
    """Everything the floor's own stream decides. The loot stream may only add
    items after these (M14.1: a vault-less floor's weapon)."""
    return {rid: (r.kind, r.name, dict(r.exits),
                  [(a.id, a.name, a.hp, a.attack, a.defense) for a in r.actors])
            for rid, r in floor.rooms.items()}


def items_extend(plain, looted) -> bool:
    return all(
        [(i.id, i.name, i.damage, i.heal) for i in room.items]
        == [(i.id, i.name, i.damage, i.heal) for i in looted.rooms[rid].items][:len(room.items)]
        for rid, room in plain.rooms.items()
    )


# -- L1: a kill can leave something behind ------------------------------------


def test_a_kill_can_leave_something(game, monkeypatch):
    engine, state, _ = game
    always_drop(monkeypatch)
    state.room.actors.append(mob())
    state.room.items.clear()
    state.rng = Rigged(20)

    events = list(engine.turn("attack offcut"))
    assert any("leaves behind" in t for t in texts(events))
    assert state.room.items, "the drop lies on the floor, to be taken"


def test_a_judge_kill_drops_too(tmp_path, theme, monkeypatch):
    """Two paths kill. A drop written into only one would be missing from the
    other -- the recurring bug shape."""
    always_drop(monkeypatch)
    client = FakeClient(structured_result={
        "plausible": True, "difficulty": 5, "affects": "enemy", "kind": "damage",
        "magnitude": 6, "reason": "It goes down."})
    settings = Settings()
    store = Store(tmp_path / "w.db")
    state = new_run(store, theme, settings, seed=7)
    engine = Engine(state, store, settings, theme, client=client)
    list(engine.begin())
    state.room.actors.append(mob())
    state.room.items.clear()
    state.rng = Rigged(20)

    events = list(engine.turn("jump"))
    assert any("leaves behind" in t for t in texts(events))
    store.close()


def test_the_boss_leaves_something_more_often_than_its_tier(theme):
    """Designed as "always"; measured at two floors of the curve on its own."""
    def rate(tier):
        boss = mob(archetype=tier, boss=True)
        return sum(loot.drop_for(boss, theme, 3, random.Random(s)) is not None
                   for s in range(400)) / 400

    weak = rate("weak")
    assert abs(weak - loot.BOSS_DROP_CHANCE) < 0.08
    assert weak > loot.DROP_CHANCE["weak"]
    # Never below its own tier (M14.1): the boss rate fell under the strong tier's.
    assert rate("strong") >= loot.DROP_CHANCE["strong"] - 0.08


def test_an_npc_leaves_nothing(theme):
    assert loot.drop_for(mob(hostile=False, boss=True), theme, 3, random.Random(0)) is None


def test_every_drop_comes_from_the_theme(theme):
    """The model never names loot: every drop is a name the theme wrote."""
    names = {e[0] for pool in theme.items.values() for e in pool}
    names |= {e[0] for tier in theme.drops.values() for pool in tier.values() for e in pool}
    dropped = 0
    for s in range(200):
        tier = random.Random(s).choice(["weak", "normal", "strong"])
        item = loot.drop_for(mob(archetype=tier, boss=True), theme, 4, random.Random(s))
        if item is not None:
            dropped += 1
            assert item.name in names
    assert dropped > 50


def test_a_themes_drop_table_is_used(theme):
    names = {d.name for s in range(200)
             if (d := loot.drop_for(mob(archetype="strong", boss=True), theme, 3,
                                    random.Random(s))) is not None}
    assert names & {"the Overprint's edge", "a compound blade"}


# -- L2: search can turn something up -----------------------------------------


@pytest.mark.parametrize("pack", ["simulacra", "hardpan"])
def test_the_loot_stream_does_not_move_the_floor(pack):
    """Existing worlds regenerate as they were: caches draw from their own
    stream, after everything else."""
    theme = Theme.load(pack)
    for ws in (1, 42, 12002):
        for d in range(1, 9):
            plain = generate_floor(d, theme, floor_rng(ws, d))
            looted = generate_floor(d, theme, floor_rng(ws, d),
                                    loot_rng=floor_rng(ws, d, "loot"))
            assert shape(plain) == shape(looted)
            assert items_extend(plain, looted)
            assert not any(r.cache for r in plain.rooms.values())


def test_the_floor_streams_seed_is_unchanged():
    assert floor_rng(5, 3).random() == random.Random("5:3").random()
    assert floor_rng(5, 3, "loot").random() != floor_rng(5, 3).random()


def test_caches_turn_up_every_few_floors(theme):
    counts = [
        sum(len(r.cache) for r in generate_floor(
            d, theme, floor_rng(ws, d), loot_rng=floor_rng(ws, d, "loot")).rooms.values())
        for ws in range(40) for d in range(1, 9)
    ]
    assert 0.2 <= sum(counts) / len(counts) <= 1.0


def hide_peaches(state):
    room = state.room
    room.cache[:] = [Item(id="c", name="a tin of peaches", tags=("healing",), heal=4)]
    room.items.clear()
    return room


def test_a_cache_is_hidden_from_everything_until_searched(game):
    """A cache named in the summary is a target the parser fallback will lift."""
    engine, state, store = game
    room = hide_peaches(state)
    assert not any("peaches" in t for t in texts(engine._contents(room)))
    assert "peaches" not in engine._room_summary()
    narrator = Narrator(FakeClient(), engine.theme, store, Settings().narrator)
    assert "peaches" not in narrator._census(state.floor, room)


def test_a_bare_search_turns_it_up_once(game):
    engine, state, _ = game
    hide_peaches(state)
    assert any("You turn up a tin of peaches" in t for t in texts(engine.turn("search")))
    assert any(i.name == "a tin of peaches" for i in state.room.items)
    assert not any("peaches" in t for t in texts(engine.turn("search")))


# -- L3: deeper finds are worth more ------------------------------------------


def test_deeper_finds_are_worth_more():
    assert make_item([["a bent single-jack", 4]], "weapon", 1, random.Random(0)).damage == 4
    assert make_item([["a bent single-jack", 4]], "weapon", 8, random.Random(0)).damage == 7
    assert make_item([["a canteen", 6]], "healing", 8, random.Random(0)).heal == 10


def test_the_inventory_shows_what_things_are_worth(game):
    engine, state, _ = game
    state.player.inventory[:] = [
        Item(id="a", name="a bent single-jack", damage=5),
        Item(id="b", name="a canteen", heal=6),
        Item(id="c", name="a brass tag"),
    ]
    lines = texts(engine.turn("inventory"))
    assert "  a bent single-jack (damage 5)" in lines
    assert "  a canteen (heals 6)" in lines
    assert "  a brass tag" in lines


# -- L4: take all, and loot that loots ------------------------------------------


def test_take_all(game):
    engine, state, _ = game
    state.room.items[:] = [Item(id="a", name="a canteen", heal=6),
                           Item(id="b", name="a knife", damage=3)]
    taken = [e.item for e in engine.turn("take all") if isinstance(e, ItemTaken)]
    assert taken == ["a canteen", "a knife"]
    assert not state.room.items


def test_loot_takes_when_there_is_something_and_searches_when_not(game):
    engine, state, _ = game
    state.room.items[:] = [Item(id="a", name="a canteen", heal=6)]
    state.room.cache.clear()
    assert any(isinstance(e, ItemTaken) for e in engine.turn("loot"))
    assert any(isinstance(e, Notice) and "nothing more" in e.text for e in engine.turn("loot"))


def test_looting_a_kill_by_name_takes_what_it_left(game, monkeypatch):
    """What a kill drops isn't named after it -- `loot the offcut` takes it anyway."""
    engine, state, _ = game
    always_drop(monkeypatch)
    state.room.actors.append(mob())
    state.room.items.clear()
    state.rng = Rigged(20)
    list(engine.turn("attack offcut"))
    assert any(isinstance(e, ItemTaken) for e in engine.turn("loot the offcut"))


# -- L6: an NPC is not a bank --------------------------------------------------


def test_an_npc_holds_three_things_at_most(tmp_path, theme):
    settings = Settings()
    store = Store(tmp_path / "w.db")
    client = FakeClient(script=["Thank you."])
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator), client=client)
    list(engine.begin())
    state.room_id = next(r.id for r in state.floor.rooms.values()
                         if any(a.id == "npc:archivist" for a in r.actors))
    for i in range(loot.HOLD_LIMIT):
        dealings.take(store, "npc:archivist", Item(id=f"h{i}", name=f"thing {i}"))
    state.player.inventory.append(Item(id="x", name="a jar of clean water", heal=6))

    before = list(client.calls)
    events = list(engine.turn("give water to archivist"))
    assert client.calls == before, "full hands cost no model call"
    assert any(i.id == "x" for i in state.player.inventory)
    assert "My hands are full." in "".join(texts(events))
    store.close()


# -- L5: the dead leave their packs --------------------------------------------


def die_holding(engine, state, items):
    state.player.inventory[:] = items
    list(engine._die("an offcut"))


def test_the_dead_leave_their_best_three_things(tmp_path, theme):
    settings = Settings()
    store = Store(tmp_path / "w.db")
    state = new_run(store, theme, settings, seed=3)
    engine = Engine(state, store, settings, theme)
    list(engine.begin())
    died_in = state.room_id
    die_holding(engine, state, [Item(id=f"i{n}", name=f"thing {n}", heal=n) for n in range(1, 6)])
    assert [r["heal"] for r in store.node_data(loot.BONES)["items"]] == [5, 4, 3]

    # The next run through this world finds them where they fell.
    state2 = new_run(store, theme, settings)
    engine2 = Engine(state2, store, settings, theme)
    list(engine2.begin())
    room = state2.floor.rooms[died_in]
    assert {"thing 5", "thing 4", "thing 3"} <= {i.name for i in room.items}
    state2.room_id = died_in
    assert any("delver's pack" in t for t in texts(engine2._contents(room)))

    list(engine2.turn("take all"))
    assert store.node_data(loot.BONES)["items"] == [], "a taken pack is gone"
    store.close()


def test_there_is_one_pack_and_the_last_death_owns_it(tmp_path, theme):
    settings = Settings()
    store = Store(tmp_path / "w.db")
    state = new_run(store, theme, settings, seed=3)
    engine = Engine(state, store, settings, theme)
    list(engine.begin())
    die_holding(engine, state, [Item(id="a", name="first", heal=6)])
    die_holding(engine, state, [Item(id="b", name="second", heal=2)])
    assert [r["name"] for r in store.node_data(loot.BONES)["items"]] == ["second"]
    store.close()


def test_bones_never_touch_a_rooms_blob(game):
    """The room node carries M10's `found` index. A second writer on that blob
    is the shape of four bugs so far."""
    engine, state, store = game
    node = f"room:{state.room_id}"
    before = store.node_data(node)
    die_holding(engine, state, [Item(id="a", name="a canteen", heal=6)])
    assert store.node_data(node) == before
