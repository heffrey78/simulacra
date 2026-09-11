"""Procedural floor layout. Pure code, no LLM, no I/O, deterministic under seed.

This is the "code owns structure" half of the hybrid split. It runs in
microseconds and is guaranteed to produce a connected, winnable floor -- the
descent is always reachable from the entrance, because we build the spanning
path first and only then add loops.

The model never sees a layout problem. It gets asked what the floor *is*
(world.director), not how it's wired.
"""

from __future__ import annotations

import math
import random

from .model import Actor, Direction, Floor, Item, Room, RoomKind
from .theme import Theme

_TIERS = [(1, "weak"), (4, "normal"), (8, "strong")]

# Fraction of the spine the player must still walk once loops are added.
#
# Loops exist to make a floor a graph rather than a hallway, but nothing stopped
# one wiring the entrance straight to the stairs -- which skipped the vault, lair
# and shrine, since all three sit on the spine. Measured before this guard:
# ~3 steps to the descent at every depth, so deeper (larger) floors were explored
# proportionally *less*. Tying the floor to a fraction of its own spine makes the
# distance grow with depth instead of staying flat.
#
# 1.0 would forbid loops entirely; 0.0 restores the original behaviour.
MIN_TRAVERSAL = 0.7

# Rejected candidates are cheap (a BFS over <= 12 rooms), so oversample rather
# than let the guard quietly thin out the loops.
_LOOP_ATTEMPTS = 8


# Floors over which a tier fades in rather than switching on. A hard threshold
# made depth 4 a cliff -- 61% of simulated runs ended on exactly that floor,
# which reads as an artifact rather than a difficulty curve. Rolling per monster
# also lets a shallow floor hold one nastier thing.
_TIER_BAND = 3.0


def _tier_for(depth: int, rng: random.Random) -> str:
    """Pick a monster tier, fading between tiers across `_TIER_BAND` floors."""
    tier = _TIERS[0][1]
    for threshold, name in _TIERS[1:]:
        chance = (depth - (threshold - 2)) / _TIER_BAND
        if rng.random() < min(1.0, max(0.0, chance)):
            tier = name
        else:
            break
    return tier


def floor_rng(world_seed: int, depth: int, stream: str = "") -> random.Random:
    """The one stream that decides what depth `depth` looks like in this world.

    Both call sites (`new_run` for floor 1, `descend` for the rest) go through
    here, because the alternative is what M6 found: `descend` was generating
    from `state.rng` -- the *dice* stream -- so how many attacks you rolled on
    floor 1 decided the shape of floor 2. Harmless while floors were thrown
    away each run; fatal once a world seed is meant to reproduce a dungeon.

    Seeded from a string rather than `world_seed ^ depth` so adjacent depths
    don't differ by a single bit. `Random(str)` hashes with SHA-512 internally,
    so this is stable across processes and unaffected by PYTHONHASHSEED.

    `stream` names a separate sequence for the same floor (M14: "loot"). New
    kinds of content must never draw from the floor's own stream: one extra
    draw there regenerates every existing world's monsters and vault items.
    The unnamed stream's seed string is unchanged, so worlds made before M14
    are still the worlds they were.
    """
    return random.Random(f"{world_seed}:{depth}" + (f":{stream}" if stream else ""))


def generate_floor(depth: int, theme: Theme, rng: random.Random | None = None,
                   loot_rng: random.Random | None = None,
                   vault_rng: random.Random | None = None) -> Floor:
    rng = rng or random.Random(depth)
    size = min(5 + depth // 2, 12)

    # 1. A guaranteed spine: entrance -> ... -> descent. Winnability by construction.
    spine = [f"d{depth}r{i}" for i in range(size)]
    kinds = [RoomKind.CORRIDOR] * size
    kinds[0] = RoomKind.ENTRANCE
    kinds[-1] = RoomKind.DESCENT
    for slot, kind in ((size // 3, RoomKind.CHAMBER), (size // 2, RoomKind.LAIR),
                       (2 * size // 3, RoomKind.VAULT), (size - 2, RoomKind.SHRINE)):
        if 0 < slot < size - 1:
            kinds[slot] = kind

    rooms: dict[str, Room] = {}
    for rid, kind in zip(spine, kinds, strict=True):
        rooms[rid] = Room(id=rid, kind=kind, depth=depth, name=rng.choice(theme.room_names(kind)))

    dirs = [Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST]
    for a, b in zip(spine, spine[1:], strict=False):
        d = rng.choice([x for x in dirs if x not in rooms[a].exits])
        _connect(rooms[a], rooms[b], d)

    # 2. Loops, so the floor is a graph rather than a hallway. Never removes a
    #    spine edge, so the floor stays completable -- and never shortens the
    #    route to the stairs past MIN_TRAVERSAL, so it stays worth walking.
    wanted = depth // 2 + 1
    min_steps = max(2, math.ceil((size - 1) * MIN_TRAVERSAL))
    added = 0
    for _ in range(wanted * _LOOP_ATTEMPTS):
        if added >= wanted:
            break
        a, b = rng.sample(spine, 2)
        if b in rooms[a].exits.values():
            continue
        free = [x for x in dirs if x not in rooms[a].exits and x.opposite not in rooms[b].exits]
        if not free:
            continue

        direction = rng.choice(free)
        _connect(rooms[a], rooms[b], direction)
        if _distance(rooms, spine[0], spine[-1]) < min_steps:
            _disconnect(rooms[a], rooms[b], direction)  # too much of a shortcut
        else:
            added += 1

    # 3. Contents. Difficulty is code's call, names are the theme's.
    _populate(rooms, depth, theme, rng)
    _place_npcs(rooms, spine[0], depth, theme)
    # 4. Caches (M14), from their own stream and after everything else, so
    #    they cannot move a single draw the floor made before they existed.
    # 5. The vault's weapon, where the floor has no vault (M14.1). The base
    #    game rather than loot, so it has its own stream: the simulation's
    #    loot-free baseline arms these floors too.
    if vault_rng is not None:
        _arm_vaultless(rooms, depth, theme, vault_rng)
    if loot_rng is not None:
        _hide_caches(rooms, depth, theme, loot_rng)
    return Floor(depth=depth, rooms=rooms, entrance_id=spine[0])


def _connect(a: Room, b: Room, d: Direction) -> None:
    a.exits[d] = b.id
    b.exits[d.opposite] = a.id


def _disconnect(a: Room, b: Room, d: Direction) -> None:
    """Exact inverse of `_connect`. Only ever called on an edge we just added,
    whose directions were confirmed free -- so this cannot cut the spine."""
    a.exits.pop(d, None)
    b.exits.pop(d.opposite, None)


def _distance(rooms: dict[str, Room], start: str, goal: str) -> int:
    """Breadth-first hop count. Returns -1 if unreachable (never, given a spine)."""
    if start == goal:
        return 0
    seen = {start}
    frontier = [start]
    steps = 0
    while frontier:
        steps += 1
        nxt: list[str] = []
        for rid in frontier:
            for dest in rooms[rid].exits.values():
                if dest == goal:
                    return steps
                if dest not in seen:
                    seen.add(dest)
                    nxt.append(dest)
        frontier = nxt
    return -1


def _populate(rooms: dict[str, Room], depth: int, theme: Theme, rng: random.Random) -> None:
    for room in rooms.values():
        if room.kind is RoomKind.LAIR:
            room.actors.append(_monster(theme, _tier_for(depth, rng), depth, rng, boss=True))
        elif room.kind in (RoomKind.CHAMBER, RoomKind.CORRIDOR) and rng.random() < 0.35:
            room.actors.append(_monster(theme, _tier_for(depth, rng), depth, rng))

        if room.kind is RoomKind.VAULT:
            room.items.append(_item(theme, "weapon", depth, rng))
        elif rng.random() < 0.25:
            room.items.append(_item(theme, "healing", depth, rng))


# Which floor an NPC lives on is the theme pack's call now (`Npc.depth`), not a
# constant here -- the reasoning that used to live at this line moved with it.


def _place_npcs(rooms: dict[str, Room], entrance_id: str, depth: int, theme: Theme) -> None:
    """Seat the theme's roster.

    The actor id **is** the theme's graph anchor. A generated id per run would
    silently create a new NPC every time, and nothing would ever be remembered.
    """
    resident = [n for n in theme.npcs if n.depth == depth]
    if not resident:
        return

    host = next((r for r in rooms.values() if r.kind is RoomKind.SHRINE), None)
    if host is None:
        host = rooms[entrance_id]

    # Somewhere to talk, not to fight.
    host.actors = [a for a in host.actors if not a.hostile]
    for npc in resident:
        host.actors.append(Actor(
            id=npc.anchor, name=npc.name, hp=1, max_hp=1,
            attack=0, defense=99, hostile=False, archetype="npc",
        ))


def _monster(theme: Theme, tier: str, depth: int, rng: random.Random, *, boss: bool = False) -> Actor:
    pool = theme.monsters.get(tier) or [["a shape", 8, 2, 10]]
    name, hp, atk, dfn = rng.choice(pool)
    scale = 1.0 + depth * 0.05
    mult = 1.4 if boss else 1.0
    return Actor(
        id=f"mob:{rng.randrange(1 << 30):x}", name=name, archetype=tier,
        hp=int(hp * scale * mult), max_hp=int(hp * scale * mult),
        attack=int(atk * mult), defense=dfn, boss=boss,
    )


# An item is worth more the deeper it was found (M14). A heal keeps pace with
# max hp, which grows 3 a floor, and a deep weapon is worth picking up at all --
# before this, every weapon in the game hit for 3 or 4. Floor 1 values are the
# theme's own. Values only, no draws: existing worlds generate the same items,
# with deeper numbers.
ITEM_SCALE = 0.08

# Hidden caches (M14), in rooms a search is plausible in: about one every two or
# three floors. The design's 0.2 (about one a floor) cost a floor of the balance
# curve on its own; see the M14 findings.
CACHE_CHANCE = 0.1
CACHE_WEAPON_SHARE = 0.3
_CACHE_KINDS = frozenset({RoomKind.CHAMBER, RoomKind.CORRIDOR, RoomKind.SHRINE})


def scaled(value: int, depth: int) -> int:
    return round(value * (1 + depth * ITEM_SCALE))


def make_item(pool, cat: str, depth: int, rng: random.Random) -> Item:
    """One item from a theme pool -- the only way an item comes into being.

    Drops and caches (M14) come through here too, which is what keeps "the
    model never names or invents loot" true by construction. Draw order is the
    pre-M14 `_item`'s: choice, then id.
    """
    entry = rng.choice(pool or [["a nondescript thing", 0]])
    name = entry[0]
    val = scaled(int(entry[1]), depth) if len(entry) > 1 else 0
    return Item(
        id=f"item:{rng.randrange(1 << 30):x}", name=name, tags=(cat,),
        damage=val if cat == "weapon" else 0,
        heal=val if cat == "healing" else 0,
    )


def _item(theme: Theme, cat: str, depth: int, rng: random.Random) -> Item:
    return make_item(theme.items.get(cat), cat, depth, rng)


def _arm_vaultless(rooms: dict[str, Room], depth: int, theme: Theme, rng: random.Random) -> None:
    """A floor without a vault still gets the vault's weapon (M14.1).

    On floors of five or six rooms the vault's slot and the shrine's are the
    same room, and the shrine wins -- so floors 1-3 have never had a vault, and
    the third playtest fought bare-handed to floor 4. Re-roling those rooms
    would change every existing world's layout; this adds the weapon from a
    stream of its own instead, to the room that took the vault's place.

    It moves the curve: the simulated median run goes from floor 4 to 6, which
    is M3's calibration having been made on unarmed early floors. See the
    M14.1 findings.
    """
    if any(r.kind is RoomKind.VAULT for r in rooms.values()):
        return
    host = (next((r for r in rooms.values() if r.kind is RoomKind.SHRINE), None)
            or next((r for r in rooms.values() if r.kind is RoomKind.LAIR), None)
            or rooms[sorted(rooms)[0]])
    host.items.append(make_item(theme.items.get("weapon"), "weapon", depth, rng))


def _hide_caches(rooms: dict[str, Room], depth: int, theme: Theme, rng: random.Random) -> None:
    """Things a bare `search` turns up. Kept out of `room.items`, which is what
    the room listing, the parser fallback's summary and the narrator read -- so
    a cache is hidden from all three by construction, not by filtering."""
    for rid in sorted(rooms):
        room = rooms[rid]
        if room.kind in _CACHE_KINDS and rng.random() < CACHE_CHANCE:
            cat = "weapon" if rng.random() < CACHE_WEAPON_SHARE else "healing"
            room.cache.append(make_item(theme.items.get(cat), cat, depth, rng))
