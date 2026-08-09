"""Streaming REPL renderer.

The *only* module allowed to know about terminals. It consumes Events and
prints; it never touches game state. A Textual frontend is a sibling of this
file implementing the same `Renderer` protocol -- not a rewrite.

MILESTONE M1.
"""

from __future__ import annotations

from rich.console import Console

from ..engine.events import (
    Damage,
    Event,
    FloorDescended,
    Improvised,
    ItemTaken,
    Line,
    Notice,
    NpcPresent,
    ProseDelta,
    ProseEnd,
    ProseStart,
    Roll,
    RoomEntered,
    RunEnded,
    StatusChanged,
    Thinking,
)

_LINE_STYLES = {
    "normal": "",
    "dim": "dim",
    "alert": "bold red",
    "good": "green",
    "title": "bold",
}

_NOTICE_STYLES = {"info": "dim", "warn": "yellow", "error": "bold red"}

# Display order for exits. The room's dict is in generation order, which is
# deterministic but reads as random.
_EXIT_ORDER = ["north", "east", "south", "west", "up", "down"]
_EXIT_SHORT = {"north": "N", "east": "E", "south": "S", "west": "W", "up": "U", "down": "D"}


class ReplRenderer:
    """Handles Events with rich. ProseDelta prints without a newline so text
    arrives token by token; everything else is a discrete line."""

    def __init__(self, console: Console | None = None):
        # markup=False is load-bearing, not tidiness. Rich reads square brackets
        # as style tags, so the combat line "[attack: 15 vs 10 -> hit]" rendered
        # as nothing at all. Model-written prose is untrusted input for the same
        # reason -- it must never be able to inject markup. Styling still works;
        # it is passed programmatically via style=.
        self.console = console or Console(highlight=False, markup=False)
        self._streaming = False

    def handle(self, event: Event) -> None:
        # Unrecognised events are ignored on purpose. M1 never emits Roll or
        # ProseDelta; handling-by-lookup means M2 and M3 add cases without
        # breaking the loop halfway through a milestone.
        match event:
            case RoomEntered():
                self._rule()
                self.console.print(event.name, style="bold")
                if event.exits:
                    ordered = sorted(
                        event.exits,
                        key=lambda e: _EXIT_ORDER.index(e) if e in _EXIT_ORDER else 99,
                    )
                    shown = " ".join(_EXIT_SHORT.get(e, e) for e in ordered)
                    self.console.print(f"exits: {shown}", style="dim")

            case Line():
                self.console.print(event.text, style=_LINE_STYLES.get(event.style, ""))

            case ProseStart():
                self._streaming = True
                if event.speaker:
                    self.console.print(f"{event.speaker}: ", end="")

            case ProseDelta():
                # end="" plus soft_wrap is what makes streaming feel continuous
                # rather than arriving in blocks.
                self.console.print(event.text, end="", soft_wrap=True)

            case ProseEnd():
                self._streaming = False
                self.console.print()

            case ItemTaken():
                self.console.print(f"Taken: {event.item}", style="green")

            case FloorDescended():
                self._rule()
                self.console.print(
                    f"You descend to floor {event.depth}. {event.theme_name}", style="bold"
                )
                if event.goal:
                    self.console.print(event.goal, style="dim")

            case NpcPresent():
                tag = " (remembers you)" if event.remembers else ""
                self.console.print(f"{event.name} is here.{tag}", style="cyan")

            case Roll():
                mark = "hit" if event.success else "miss"
                detail = f" {event.detail}" if event.detail else ""
                self.console.print(
                    f"[{event.label}: {event.total} vs {event.target} -> {mark}]{detail}",
                    style="dim",
                )

            case Damage():
                verb = "take" if event.target == "you" else "takes"
                self.console.print(
                    f"{event.target} {verb} {event.amount} ({event.hp_left} left)", style="red"
                )

            case Improvised():
                style = "dim" if event.plausible else "yellow"
                self.console.print(event.reason, style=style)

            case StatusChanged():
                effects = f" [{', '.join(event.effects)}]" if event.effects else ""
                self.console.print(
                    f"hp {event.hp}/{event.max_hp}  depth {event.depth}{effects}", style="dim"
                )

            case Thinking():
                self.console.print(f"{event.label}...", style="dim italic")

            case Notice():
                self.console.print(event.text, style=_NOTICE_STYLES.get(event.level, "dim"))

            case RunEnded():
                self._rule()
                self.console.print(
                    f"The run ends: {event.cause}. Floor {event.depth}, {event.turns} turns.",
                    style="bold red",
                )
                if event.epitaph:
                    self.console.print(event.epitaph, style="italic")

            case _:
                pass

    def _rule(self) -> None:
        self.console.print()


def read_input(prompt: str = "> ") -> str:
    """EOF (Ctrl-D) and Ctrl-C are how people leave a REPL, not tracebacks."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return "quit"
