"""Run and game state.

MILESTONE M1.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field

from ..world.floorgen import floor_rng, generate_floor
from ..world.model import Floor, Player


@dataclass
class GameState:
    run_id: int
    player: Player
    floor: Floor
    room_id: str
    depth: int = 1
    turns: int = 0
    # Two seeds, two jobs. `world_seed` belongs to the database and decides
    # layout at every depth; `seed` is this run's, and decides dice. Before M6
    # one value did both, and `descend()` generated from the dice stream -- so
    # a combat roll on floor 1 changed the shape of floor 2.
    world_seed: int = 0
    seed: int = 0
    rng: random.Random = field(default_factory=random.Random)
    # Theme names of recent floors, fed to the director as a do-not-repeat list.
    floor_history: list[str] = field(default_factory=list)
    # NPCs the player has actually spoken to this run. They are the witnesses:
    # a memory is subject to whoever was met, which is what decides who can
    # recall it later.
    met_npcs: set[str] = field(default_factory=set)

    @property
    def room(self):
        return self.floor.room(self.room_id)


def persist_floor(store, floor: Floor, run_id: int) -> None:
    """Write a floor's **identity** into the graph.

    Since M6 this is the half of a floor that cannot be recomputed. Layout is a
    pure function of the world seed and the depth (`floorgen.floor_rng`), so
    regenerating floor 7 is cheaper than reading it back; what the model
    contributed -- the floor's name, goal and motifs, and each room's name and
    concept -- is not reproducible and is what gets stored.

    Call this **after** the director has run. Calling it before is what left
    `floor:1` named "floor 1" forever, since floor 1 was persisted in `new_run`
    and directed later in `begin()`.

    Structure is world-scoped (`run_id=None`); traversal is run-scoped. The edge
    primary key includes `run_id`, so writing `CONTAINS`/`EXIT_*` per run would
    add an identical row for the same wall on every run, forever.

    Note the node rows for `run:` and `floor:`: `Store.neighbors()` joins against
    `nodes`, so an edge whose endpoints have no node row is invisible to it.
    """
    run_node = f"run:{run_id}"
    floor_node = f"floor:{floor.depth}"

    store.upsert_node(run_node, "run", f"run {run_id}", run_id=run_id)
    store.upsert_node(
        floor_node, "floor", floor.theme_name or f"floor {floor.depth}",
        {
            **store.node_data(floor_node),
            "depth": floor.depth,
            "theme_name": floor.theme_name,
            "goal": floor.goal,
            "motifs": list(floor.motifs),
        },
        run_id=run_id,
    )
    store.link(run_node, "ENTERED", floor_node, run_id=run_id)

    for room in floor.rooms.values():
        node = f"room:{room.id}"
        store.upsert_node(
            node, "room", room.name,
            # Merged, not replaced (M11). Other systems keep state on room nodes
            # -- M10's `found` index lives here -- and rewriting the blob on
            # every run erased it, so a discovery could be replayed only in the
            # run that made it and the next run re-discovered it as something
            # new. Found by replaying the 2026-09-10 playtest against its own
            # world; the M10 test that should have caught it never called
            # begin() on run 2.
            {**store.node_data(node), "kind": room.kind.value,
             "depth": room.depth, "concept": room.concept},
            run_id=run_id,
        )
        store.link(floor_node, "CONTAINS", f"room:{room.id}")

    for room in floor.rooms.values():
        for direction, dest in room.exits.items():
            store.link(
                f"room:{room.id}", f"EXIT_{direction.value.upper()}", f"room:{dest}"
            )

    # One commit for the whole floor, not one per node.
    store.commit()


def load_floor_identity(store, floor: Floor) -> bool:
    """Re-attach a previously generated identity to a freshly built floor.

    Returns True when the floor already has one, which is the caller's signal to
    skip the ~19 s tier-3 director call entirely. That skip is the whole payoff
    of persisting floors: the director is paid once per floor for the life of
    the world instead of once per floor per run.

    Deliberately all-or-nothing on the floor's own identity: a floor node with
    no `theme_name` has never been directed, so a half-applied floor cannot
    happen. Room concepts are applied individually because the director already
    drops rooms it failed to name.
    """
    node = store.node(f"floor:{floor.depth}")
    if node is None:
        return False
    data = json.loads(node["data"] or "{}")
    if not (data.get("theme_name") or "").strip():
        return False

    floor.theme_name = data["theme_name"]
    floor.goal = data.get("goal") or ""
    floor.motifs = tuple(data.get("motifs") or ())

    for room in floor.rooms.values():
        row = store.node(f"room:{room.id}")
        if row is None:
            continue
        rd = json.loads(row["data"] or "{}")
        if name := (row["name"] or "").strip():
            room.name = name
        if concept := (rd.get("concept") or "").strip():
            room.concept = concept

    return True


def ensure_npcs(store, theme) -> None:
    """Give every roster NPC a home in the graph, before anyone has met them.

    Until M7 an NPC got a `nodes` row only once it had been spoken to, which
    made it a thing that appeared when observed rather than a resident of the
    world. Nothing can be given a gift, moved, or hold an opinion of the player
    until it exists independently of being looked at.

    Idempotent in two different ways, and the difference matters. The node's
    `data` is written only on creation -- rewriting it would clobber an NPC that
    has since moved. Authored canon is reconciled every time, because the theme
    pack is its source of truth: adding a line to the roster should reach worlds
    that already exist, without mutating the rows already there.
    """
    for npc in theme.npcs:
        if store.node(npc.anchor) is None:
            store.upsert_node(
                npc.anchor, "npc", npc.name,
                {
                    "home_depth": npc.depth,
                    "room_id": None,
                    "disposition": 0,
                    "inventory": [],
                    "last_canon_run": None,
                },
            )
        known = {r["text"] for r in store.canon(npc.anchor, provenance="authored")}
        for text in npc.canon:
            if text not in known:
                store.add_canon(npc.anchor, text, "authored")
    store.commit()


def place_npcs(store, floor, theme) -> None:
    """Put each resident NPC where the *world* says it is.

    `floorgen` still decides a default home, because it must stay pure -- no
    store, no I/O. This inverts the dependency one layer up: a stored `room_id`
    wins, and an NPC that has never been placed has its generated room written
    back. That is what makes NPC movement (M9) a state update rather than a
    floorgen rewrite.
    """
    for npc in theme.npcs:
        if npc.depth != floor.depth:
            continue
        node = store.node(npc.anchor)
        if node is None:
            continue

        here = next(
            (r for r in floor.rooms.values() if any(a.id == npc.anchor for a in r.actors)),
            None,
        )
        if here is None:
            continue

        data = json.loads(node["data"] or "{}")
        stored = data.get("room_id")

        if stored and stored in floor.rooms:
            if stored != here.id:
                actor = next(a for a in here.actors if a.id == npc.anchor)
                here.actors.remove(actor)
                floor.rooms[stored].actors.append(actor)
        else:
            data["room_id"] = here.id
            store.upsert_node(npc.anchor, "npc", npc.name, data)
    store.commit()


def new_run(store, theme, settings, seed: int | None = None) -> GameState:
    """Open a run against an existing world: allocate the run row, build floor 1.

    `seed` is context-dependent, and `Store.claim_world_seed` owns the rule: on
    an unplayed world it sets the world seed (so `--seed 42` still means "give
    me this dungeon"), and on a played one it is only this run's dice.

    Floor 1 is **not** persisted here. Identity is written after the director
    has run, which happens in `Engine.begin()` -- writing it here is what left
    `floor:1` named "floor 1" forever while every deeper floor got its real name.
    """
    world_seed = store.claim_world_seed(seed)
    run_seed = random.randrange(1 << 30) if seed is None else int(seed)

    ensure_npcs(store, theme)

    run_id = store.start_run(theme.name, seed=run_seed)
    floor = generate_floor(1, theme, floor_rng(world_seed, 1))
    place_npcs(store, floor, theme)

    return GameState(
        run_id=run_id,
        player=Player(),
        floor=floor,
        room_id=floor.entrance_id,
        depth=1,
        world_seed=world_seed,
        seed=run_seed,
        rng=random.Random(run_seed),
    )
