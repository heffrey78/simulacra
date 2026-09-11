"""The engine/renderer boundary.

The engine never prints. It yields Events, and a Renderer decides what they look
like. This is the single seam that makes "REPL now, Textual later" a matter of
writing a second renderer instead of rewriting the game.

Rule: if the engine ever needs to know it's talking to a terminal, the seam has
leaked. Prose arrives as a stream of ProseDelta so a renderer can paint it token
by token; a renderer that doesn't care can just concatenate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable


class Event:
    """Marker base. Renderers should ignore events they don't recognise."""


# -- narration -------------------------------------------------------------


@dataclass(frozen=True)
class ProseStart(Event):
    """A block of streamed prose is beginning."""

    channel: Literal["room", "npc", "combat", "system", "detail"] = "room"
    speaker: str | None = None


@dataclass(frozen=True)
class ProseDelta(Event):
    text: str


@dataclass(frozen=True)
class ProseEnd(Event):
    pass


@dataclass(frozen=True)
class Line(Event):
    """A complete, non-streamed line of text."""

    text: str
    style: Literal["normal", "dim", "alert", "good", "title"] = "normal"


# -- world -----------------------------------------------------------------


@dataclass(frozen=True)
class RoomEntered(Event):
    room_id: str
    name: str
    exits: tuple[str, ...]
    first_visit: bool
    # M5. The direction travelled to get here; None when arrival wasn't a move
    # (`begin`, `descend`, `look`). A renderer that lays rooms out on a grid
    # cannot derive this from anything else it is told, and the engine already
    # knows it -- it was simply being thrown away. Defaulted so every existing
    # construction site and test keeps working.
    via: str | None = None
    # M15. The exits that lead to a room not yet visited this run. The third
    # playtest spent 18 of floor 6's 73 commands on "There are no stairs here."
    # while looping five rooms: the way down was behind the one exit never
    # taken, and the exits line couldn't say which one that was.
    unexplored: tuple[str, ...] = ()


@dataclass(frozen=True)
class FloorDescended(Event):
    depth: int
    theme_name: str
    goal: str


@dataclass(frozen=True)
class FloorNamed(Event):
    """The floor a run *starts* on has its identity (M13).

    `FloorDescended` is only emitted by a descent, so floor 1's tier-3 name and
    goal never reached the TUI's header (M5's recorded gap). Emitting a descent
    from `begin()` would print "You descend to floor 1" in the REPL; this is the
    separate event M5 asked for, and renderers without a header ignore it.
    """

    depth: int
    theme_name: str
    goal: str


@dataclass(frozen=True)
class ItemTaken(Event):
    item: str


@dataclass(frozen=True)
class NpcPresent(Event):
    npc_id: str
    name: str
    remembers: bool  # true when the memory layer surfaced a prior-run recollection


# -- resolution ------------------------------------------------------------


@dataclass(frozen=True)
class Roll(Event):
    """A dice resolution, surfaced so the player can see the game is honest."""

    label: str
    total: int
    target: int
    success: bool
    detail: str = ""


@dataclass(frozen=True)
class Damage(Event):
    target: str
    amount: int
    hp_left: int


@dataclass(frozen=True)
class Improvised(Event):
    """The LLM judge ruled on an action the parser had no verb for."""

    action: str
    plausible: bool
    reason: str


@dataclass(frozen=True)
class StatusChanged(Event):
    hp: int
    max_hp: int
    depth: int
    effects: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunEnded(Event):
    cause: str
    depth: int
    turns: int
    epitaph: str = ""


# -- meta ------------------------------------------------------------------


@dataclass(frozen=True)
class Thinking(Event):
    """Cover for an unavoidable blocking call (tier 3 floor generation).

    Renderers should show a spinner. Nothing else in the loop should ever need
    this -- if it does, that call belongs in the prefetcher.
    """

    label: str


@dataclass(frozen=True)
class Notice(Event):
    text: str
    level: Literal["info", "warn", "error"] = "info"


@dataclass(frozen=True)
class Transcript(Event):
    """Everything worth committing to long-term memory from this turn.

    The engine emits it; the memory layer consumes it. Keeping it an event means
    memory is an observer, not a dependency of the turn loop.
    """

    summary: str
    kind: Literal["event", "dialogue", "death", "discovery"] = "event"
    subjects: tuple[str, ...] = ()
    tags: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class Renderer(Protocol):
    def handle(self, event: Event) -> None: ...
