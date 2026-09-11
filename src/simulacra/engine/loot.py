"""Loot (M14): what a kill leaves, and what the dead leave behind.

Tier 0 throughout. Every item comes from a theme pool through seeded dice --
`floorgen.make_item` is the only constructor -- and the model never names or
invents one. That is M9's rule (an NPC can only hand over what exists) and
M10's (a discovery is scenery) extended to everything the player can carry.

The rates are measured, not chosen (docs/tasks/M14-looting-tasks.md, Findings).
At these, M3's simulated median run moves by one floor at most, and
`test_combat.py` holds it there.
"""

from __future__ import annotations

import random

from ..world.floorgen import make_item
from ..world.model import Actor, Item

# The design's 25/40/60%, with a boss that always dropped, moved the simulated
# median run from floor 4 to 7. Each source alone added a floor or two, and
# together they pushed runs up to the wall where the strong tier arrives. These
# are the most generous rates found that keep the median within one floor, on
# both themes.
DROP_CHANCE = {"weak": 0.15, "normal": 0.25, "strong": 0.4}
WEAPON_SHARE = 0.3
# The lair's occupant drops at least this often, and leans toward a weapon. It
# was going to be "always" -- which on its own cost two floors of the curve,
# since a heal right after the floor's hardest fight is when a heal is worth
# most. M14 shipped 0.5 to arm floors 1-3, which had no vault. M14.1 gave those
# floors their weapon directly, and that alone moved the base curve two floors;
# 0.35 is what keeps loot within one floor of the armed game.
BOSS_DROP_CHANCE = 0.35
BOSS_WEAPON_SHARE = 0.6

# The last death's pack: the best few things carried. One per world, on a node
# of its own -- sharing a room node's blob with M10's `found` index is how the
# M11 bug happened, and one fixed id makes "one pack per world" structural.
PACK_LIMIT = 3
BONES = "bones:last"
BONES_TAG = "bones"

# `take all`, `take everything`: what the player types for the whole room.
ALL_WORDS = frozenset({"all", "everything", "it all", "all of it"})

# What one NPC may hold. Holdings are world-scoped and a warm NPC may hand them
# back, so without a ceiling every drop, cache and pack could be banked.
HOLD_LIMIT = 3


def drop_for(actor: Actor, theme, depth: int, rng: random.Random) -> Item | None:
    """What a kill leaves behind, if anything. The run's dice, never the floor's."""
    if not actor.hostile:
        return None
    tier = DROP_CHANCE.get(actor.archetype, 0.0)
    # A boss never drops less often than its own tier would: 0.35 is below the
    # strong tier's rate.
    chance = max(BOSS_DROP_CHANCE, tier) if actor.boss else tier
    if rng.random() >= chance:
        return None
    share = BOSS_WEAPON_SHARE if actor.boss else WEAPON_SHARE
    cat = "weapon" if rng.random() < share else "healing"
    pool = (theme.drops.get(actor.archetype) or {}).get(cat) or theme.items.get(cat)
    return make_item(pool, cat, depth, rng)


def worth(item) -> int:
    return max(item.damage, item.heal)


def describe(item) -> str:
    """An item's number, where it has one. A weapon's is the base of its damage
    roll, not a guaranteed hit, so it is not called one."""
    if item.damage > 0:
        return f" (damage {item.damage})"
    if item.heal > 0:
        return f" (heals {item.heal})"
    return ""


# -- bones -------------------------------------------------------------------


def leave_pack(store, *, room_id: str, depth: int, run_id: int, inventory) -> None:
    """A death replaces the world's pack -- even with nothing, since the pack
    is the *last* delver's, not the best one there has ever been."""
    best = sorted(inventory, key=worth, reverse=True)[:PACK_LIMIT]
    items = [{"id": i.id, "name": i.name, "damage": i.damage, "heal": i.heal} for i in best]
    store.upsert_node(BONES, "bones", "a delver's pack",
                      {"room_id": room_id, "depth": depth, "run_id": run_id, "items": items},
                      run_id=run_id)
    store.commit()


def pack_on(store, depth: int) -> tuple[str, list[Item]] | None:
    """Where the pack is and what is in it, if it is on this floor."""
    data = store.node_data(BONES)
    if data.get("depth") != depth or not data.get("items"):
        return None
    items = [
        Item(id=str(r["id"]), name=str(r["name"]), tags=(BONES_TAG,),
             damage=int(r.get("damage", 0)), heal=int(r.get("heal", 0)))
        for r in data["items"] if isinstance(r, dict) and r.get("id")
    ]
    return (str(data.get("room_id")), items) if items else None


def take_from_pack(store, item_id: str) -> None:
    data = store.node_data(BONES)
    if not data:
        return
    data["items"] = [r for r in data.get("items") or []
                     if isinstance(r, dict) and r.get("id") != item_id]
    store.set_node_data(BONES, data)
