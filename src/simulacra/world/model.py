"""World data types.

These are plain data. Everything expensive -- prose, NPC voice, floor concept --
is *attached* to them, never required to construct them. That split is what lets
the floor exist and be walkable instantly while the model is still writing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Direction(StrEnum):
    NORTH = "north"
    SOUTH = "south"
    EAST = "east"
    WEST = "west"
    DOWN = "down"
    UP = "up"

    @property
    def opposite(self) -> Direction:
        return _OPPOSITE[self]

    @classmethod
    def parse(cls, token: str) -> Direction | None:
        return _ALIASES.get(token.strip().lower())


_OPPOSITE = {
    Direction.NORTH: Direction.SOUTH,
    Direction.SOUTH: Direction.NORTH,
    Direction.EAST: Direction.WEST,
    Direction.WEST: Direction.EAST,
    Direction.DOWN: Direction.UP,
    Direction.UP: Direction.DOWN,
}

_ALIASES = {
    **{d.value: d for d in Direction},
    "n": Direction.NORTH, "s": Direction.SOUTH,
    "e": Direction.EAST, "w": Direction.WEST,
    "d": Direction.DOWN, "u": Direction.UP,
    "down": Direction.DOWN, "up": Direction.UP,
}


class RoomKind(StrEnum):
    """Structural role, decided by code. The theme pack decides what it's *called*."""

    ENTRANCE = "entrance"
    CORRIDOR = "corridor"
    CHAMBER = "chamber"
    VAULT = "vault"        # holds the floor's reward
    LAIR = "lair"          # holds the floor's threat
    SHRINE = "shrine"      # the floor's "special" room; director names it
    DESCENT = "descent"    # stairs down


@dataclass
class Item:
    id: str
    name: str
    tags: tuple[str, ...] = ()
    damage: int = 0
    heal: int = 0
    description: str = ""


@dataclass
class Actor:
    """A monster or NPC. `hostile` decides which subsystem drives it."""

    id: str
    name: str
    hp: int
    max_hp: int
    attack: int = 2
    defense: int = 10
    hostile: bool = True
    archetype: str = ""
    # The lair's occupant. Always leaves something behind (M14).
    boss: bool = False
    # Populated by the memory layer at encounter time, not at generation time.
    recollections: list[str] = field(default_factory=list)


@dataclass
class Room:
    id: str
    kind: RoomKind
    depth: int
    exits: dict[Direction, str] = field(default_factory=dict)
    items: list[Item] = field(default_factory=list)
    actors: list[Actor] = field(default_factory=list)
    # Hidden until a bare `search` (M14). A separate list rather than a flag on
    # items: everything that must not see a cache reads `items` and nothing else.
    cache: list[Item] = field(default_factory=list)

    # Set by the director (tier 3, once per floor) -- a short concept the
    # narrator expands. Cheap to generate, cheap to store, keeps rooms distinct.
    concept: str = ""
    name: str = ""

    # Set by the narrator (tier 2, prefetched). Cached so a revisit is free.
    prose: str = ""
    visited: bool = False

    @property
    def described(self) -> bool:
        return bool(self.prose)


@dataclass
class Floor:
    depth: int
    rooms: dict[str, Room]
    entrance_id: str

    # Director output. Absent until the tier-3 call lands; the floor is fully
    # walkable without it.
    theme_name: str = ""
    goal: str = ""
    motifs: tuple[str, ...] = ()

    def room(self, room_id: str) -> Room:
        return self.rooms[room_id]

    def neighbors(self, room_id: str) -> list[Room]:
        return [self.rooms[rid] for rid in self.rooms[room_id].exits.values() if rid in self.rooms]


# Progression, granted on descending. Without it the player's damage is fixed
# while monster HP scales with depth, so the run caps out around floor 4 and
# "endless" means nothing -- measured at 20% survival on depth 4 and 0% from
# depth 8 before this existed. Descending is the only thing that grants it, so
# the reward for risk is the ability to take more risk.
HP_PER_FLOOR = 3
DESCENT_HEAL = 6
ATTACK_EVERY = 3  # floors per +1 attack


@dataclass
class Player:
    name: str = "the delver"
    hp: int = 20
    max_hp: int = 20
    attack: int = 3
    defense: int = 12
    inventory: list[Item] = field(default_factory=list)
    effects: dict[str, int] = field(default_factory=dict)  # name -> turns remaining

    @property
    def alive(self) -> bool:
        return self.hp > 0

    def on_descend(self, depth: int) -> None:
        """Catch your breath and harden slightly. Never a full heal -- attrition
        across floors is what eventually ends a run."""
        self.max_hp += HP_PER_FLOOR
        if depth % ATTACK_EVERY == 0:
            self.attack += 1
        self.hp = min(self.max_hp, self.hp + DESCENT_HEAL)
