"""Run and game state.

MILESTONE M1.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..world.floorgen import generate_floor
from ..world.model import Floor, Player


@dataclass
class GameState:
    run_id: int
    player: Player
    floor: Floor
    room_id: str
    depth: int = 1
    turns: int = 0
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
    """Write a floor into the graph.

    Nothing reads this until M4. It is done now anyway because it costs ten lines
    here and a painful backfill later -- by the time the memory layer exists, the
    interesting runs will already have happened.

    Note the node rows for `run:` and `floor:`: `Store.neighbors()` joins against
    `nodes`, so an edge whose endpoints have no node row is invisible to it.
    """
    run_node = f"run:{run_id}"
    floor_node = f"floor:{floor.depth}"

    store.upsert_node(run_node, "run", f"run {run_id}", run_id=run_id)
    store.upsert_node(
        floor_node, "floor", floor.theme_name or f"floor {floor.depth}",
        {"depth": floor.depth}, run_id=run_id,
    )
    store.link(run_node, "ENTERED", floor_node, run_id=run_id)

    for room in floor.rooms.values():
        store.upsert_node(
            f"room:{room.id}", "room", room.name,
            {"kind": room.kind.value, "depth": room.depth}, run_id=run_id,
        )
        store.link(floor_node, "CONTAINS", f"room:{room.id}", run_id=run_id)

    for room in floor.rooms.values():
        for direction, dest in room.exits.items():
            store.link(
                f"room:{room.id}", f"EXIT_{direction.value.upper()}",
                f"room:{dest}", run_id=run_id,
            )

    # One commit for the whole floor, not one per node.
    store.commit()


def new_run(store, theme, settings, seed: int | None = None) -> GameState:
    """Open a run: allocate the run row, build floor 1, persist it, return state."""
    if seed is None:
        seed = random.randrange(1 << 30)

    run_id = store.start_run(theme.name, seed=seed)
    # The state RNG and the floor RNG are separate streams off the same seed, so
    # that in-game rolls can never shift floor layout (or vice versa).
    floor = generate_floor(1, theme, random.Random(seed))
    persist_floor(store, floor, run_id)

    return GameState(
        run_id=run_id,
        player=Player(),
        floor=floor,
        room_id=floor.entrance_id,
        depth=1,
        seed=seed,
        rng=random.Random(seed ^ 0x5EED),
    )
