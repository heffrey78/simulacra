"""Textual frontend -- a sibling of `repl.py`, not a rewrite.

This is the milestone the event seam was designed for. It consumes exactly the
same `Event` stream the REPL does, and the engine has no idea it exists. The
only thing M5 asked of the engine is `RoomEntered.via` (see events.py), because
a grid layout genuinely cannot derive the direction travelled from anything else
it is told.

**The engine is a synchronous generator; Textual is async. That is the whole
problem.** `engine.turn()` blocks -- ~3.5 s for room prose, ~18 s for a tier-3
descent. Called on the event loop it freezes everything, including the spinner
that exists to cover exactly that wait. So the turn runs in a worker *thread*
and each event is posted to the UI as it is yielded:

    for event in self.engine.turn(text):
        self.call_from_thread(self._dispatch, event)

Two consequences are designed around rather than discovered:

- **Per-event posting is what makes streaming work.** `list(engine.turn(text))`
  would look fine and silently undo M2 -- the prose would arrive all at once
  after a six second wait instead of outrunning the reader at 15 tok/s.
- **`GameState` is being mutated on the worker thread**, so no panel may read
  it. This app is never handed `state` at all: `play_tui` passes the engine and
  nothing else, which turns "don't read state from the UI thread" from a rule
  someone has to remember into something you cannot express.

MILESTONE M5.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, RichLog, Static

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

# Same display order and abbreviations as the REPL. The two frontends showing
# exits in different orders would be a small, permanent papercut.
_EXIT_ORDER = ["north", "east", "south", "west", "up", "down"]
_EXIT_SHORT = {"north": "N", "east": "E", "south": "S", "west": "W", "up": "U", "down": "D"}

_LINE_STYLES = {
    "normal": "",
    "dim": "dim",
    "alert": "bold red",
    "good": "green",
    "title": "bold",
}

_NOTICE_STYLES = {"info": "dim", "warn": "yellow", "error": "bold red"}

# Tokens arrive at ~15/s. Repainting per token is 15 full relayouts a second for
# no perceptible gain over ~8, on a box whose cores are busy doing the
# inference that produced them. See T7 in the task doc.
_FLUSH_INTERVAL = 0.12

# Only ever running while a Thinking beat is open -- never at idle. It covers
# the one unavoidable 18 s block in the game, which is precisely when a frozen
# screen is indistinguishable from a crash.
_SPINNER_INTERVAL = 0.2
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_MOVE_KEYS = {"n": "north", "s": "south", "e": "east", "w": "west"}


# -- the map ---------------------------------------------------------------


_DELTA = {"north": (0, -1), "south": (0, 1), "east": (1, 0), "west": (-1, 0)}

_HERE = "▣"
_SEEN = "▢"
_STUB = "·"


class FogMap:
    """Dead reckoning from `RoomEntered` alone.

    Kept free of Textual so it can be tested as what it is: a small piece of
    arithmetic. Building it from events rather than `state.floor` avoids the
    worker-thread race *and* gives fog of war for nothing -- you draw what you
    were told about, because that is literally all you know.

    Note that floors are not planar. `floorgen` picks each connection's
    direction at random, so two rooms can honestly dead-reckon onto the same
    square. One square holds one room: the newer sighting wins and the older is
    forgotten, which is what happens to a hand-drawn map too.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.pos: dict[str, tuple[int, int]] = {}
        self.cell: dict[tuple[int, int], str] = {}
        self.exits: dict[str, tuple[str, ...]] = {}
        self.current: str | None = None

    def enter(self, room_id: str, exits: Iterable[str], via: str | None) -> None:
        if room_id in self.pos:
            pass                                    # a revisit keeps its square
        elif via in _DELTA and self.current is not None:
            cx, cy = self.pos[self.current]
            dx, dy = _DELTA[via]
            self._claim(room_id, (cx + dx, cy + dy))
        else:
            # An arrival with no direction to reckon from: a new floor, or a
            # vertical move. Nothing already drawn relates to it, so start the
            # sheet again.
            self.reset()
            self._claim(room_id, (0, 0))
        self.exits[room_id] = tuple(exits)
        self.current = room_id

    def _claim(self, room_id: str, xy: tuple[int, int]) -> None:
        displaced = self.cell.get(xy)
        if displaced is not None and displaced != room_id:
            self.pos.pop(displaced, None)
            self.exits.pop(displaced, None)
        self.cell[xy] = room_id
        self.pos[room_id] = xy

    # -- rendering ---------------------------------------------------------

    def _stubs(self) -> set[tuple[int, int]]:
        """Squares a known exit leads to that we have not stood in."""
        out: set[tuple[int, int]] = set()
        for room_id, exits in self.exits.items():
            x, y = self.pos[room_id]
            for direction in exits:
                if direction not in _DELTA:
                    continue
                dx, dy = _DELTA[direction]
                if (x + dx, y + dy) not in self.cell:
                    out.add((x + dx, y + dy))
        return out

    def _linked(self, a: tuple[int, int], b: tuple[int, int], direction: str) -> bool:
        """Is there a passage between two squares? Either end may know about it:
        the far room is a stub we have never entered, so only the near one can
        speak for the edge."""
        near = self.cell.get(a)
        far = self.cell.get(b)
        if near is not None and direction in self.exits.get(near, ()):
            return True
        opposite = {"north": "south", "south": "north", "east": "west", "west": "east"}
        return far is not None and opposite[direction] in self.exits.get(far, ())

    def render_lines(self) -> list[str]:
        if not self.pos:
            return []

        squares = set(self.cell) | self._stubs()
        xs = [x for x, _ in squares]
        ys = [y for _, y in squares]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)

        lines: list[str] = []
        for y in range(y0, y1 + 1):
            row = ""
            for x in range(x0, x1 + 1):
                room = self.cell.get((x, y))
                if room is None:
                    row += _STUB if (x, y) in squares else " "
                else:
                    row += _HERE if room == self.current else _SEEN
                if x < x1:
                    row += "─" if self._linked((x, y), (x + 1, y), "east") else " "
            lines.append(row)

            if y < y1:
                gap = ""
                for x in range(x0, x1 + 1):
                    gap += "│" if self._linked((x, y), (x, y + 1), "south") else " "
                    if x < x1:
                        gap += " "
                lines.append(gap)
        return lines


class MapPanel(Static):
    """Thin: it owns a FogMap and paints it. All the thinking is in FogMap."""

    def __init__(self, **kwargs) -> None:
        super().__init__("", markup=False, **kwargs)
        self.map = FogMap()

    def on_mount(self) -> None:
        self.border_title = "map"

    def entered(self, event: RoomEntered) -> None:
        self.map.enter(event.room_id, event.exits, event.via)
        self.repaint()

    def descended(self) -> None:
        self.map.reset()
        self.repaint()

    def repaint(self) -> None:
        lines = self.map.render_lines()
        body = Text("\n".join(lines))
        if not lines:
            body = Text("unmapped", style="dim")
        self.update(body)


# -- the status panel ------------------------------------------------------


_BAR_WIDTH = 12


class StatusPanel(Static):
    """Driven entirely by `StatusChanged`, plus `FloorDescended` for the header
    and `RoomEntered` for the room name. Nothing else feeds it, and it never
    reads game state -- the worker thread is mutating that."""

    def __init__(self, **kwargs) -> None:
        super().__init__("", markup=False, **kwargs)
        self.hp = 0
        self.max_hp = 0
        self.depth = 1
        self.effects: tuple[str, ...] = ()
        self.room = ""
        self.floor_name = ""
        # The tier-3 director writes this once per floor and the REPL lets it
        # scroll away after one line. Here it stays on screen, which is most of
        # what the call was for.
        self.goal = ""

    def on_mount(self) -> None:
        self.border_title = "status"

    def status(self, event: StatusChanged) -> None:
        self.hp, self.max_hp = event.hp, event.max_hp
        self.depth, self.effects = event.depth, event.effects
        self.repaint()

    def hurt(self, hp_left: int) -> None:
        """Combat damage does not emit `StatusChanged` -- the engine only sends
        one when effects tick or an item is used. In the REPL that is invisible,
        because the status line is a thing that scrolls past. A *panel* that
        still reads 23/23 while the log says the killing blow landed is simply
        wrong, and it was wrong for the whole of the first live run.

        The fix is not an engine change and not a state read: `Damage.hp_left`
        is already in the stream, and for `target == "you"` it is exactly the
        player's hp. This is the one place something other than StatusChanged
        feeds this panel, and it feeds it a number it was handed.
        """
        self.hp = hp_left
        self.repaint()

    def entered(self, event: RoomEntered) -> None:
        self.room = event.name
        self.repaint()

    def descended(self, event: FloorDescended) -> None:
        self.floor_name, self.goal = event.theme_name, event.goal
        self.repaint()

    def repaint(self) -> None:
        out = Text()
        if self.room:
            out.append(self.room + "\n", style="bold")

        if self.max_hp:
            share = self.hp / self.max_hp
            # An hp bar that changes colour is ten lines the REPL cannot do, and
            # is most of why a panelled UI is nicer than a scrolling one.
            style = "green" if share > 2 / 3 else "yellow" if share > 1 / 3 else "bold red"
            filled = max(0, min(_BAR_WIDTH, round(share * _BAR_WIDTH)))
            out.append("hp ")
            out.append("▓" * filled, style=style)
            out.append("░" * (_BAR_WIDTH - filled), style="dim")
            out.append(f" {self.hp}/{self.max_hp}\n", style=style)

        out.append(f"floor {self.depth}", style="dim")
        # With no model the director never runs, and `descend` falls back to
        # "floor N" as the theme name -- which would print twice.
        if self.floor_name and self.floor_name != f"floor {self.depth}":
            out.append(f"  {self.floor_name}", style="dim")
        out.append("\n")

        if self.effects:
            # Names only: StatusChanged carries no turn counts, and inventing a
            # number by reading the player would be the exact race this design
            # exists to avoid.
            out.append(", ".join(self.effects) + "\n", style="magenta")
        if self.goal:
            out.append("\n" + self.goal, style="italic dim")
        self.update(out)


# -- input -----------------------------------------------------------------


class CommandInput(Input):
    """Adds two things the REPL has never had: command history, and single-key
    movement when the line is empty.

    A movement key *fills in the line and submits it*, so what reaches the
    engine is a string the player could have typed. Bypassing the parser here
    would let the TUI and the REPL diverge in behaviour and would stop
    exercising stage-2 inference.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # No blink: it is the only thing in this UI that repaints at idle, and
        # idle is exactly when the prefetcher is narrating the next three rooms.
        # A reactive, not a constructor argument, in Textual 8.
        self.cursor_blink = False
        self.history: list[str] = []
        self._at = 0

    def remember(self, text: str) -> None:
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
        self._at = len(self.history)

    async def _on_key(self, event) -> None:
        if not self.value and event.key in _MOVE_KEYS:
            event.stop()
            event.prevent_default()
            self.value = _MOVE_KEYS[event.key]
            await self.action_submit()
            return

        if event.key in ("up", "down") and self.history:
            event.stop()
            event.prevent_default()
            self._at = max(0, min(len(self.history),
                                  self._at + (-1 if event.key == "up" else 1)))
            self.value = self.history[self._at] if self._at < len(self.history) else ""
            self.cursor_position = len(self.value)
            return

        await super()._on_key(event)


# -- the app ---------------------------------------------------------------


class SimulacraApp(App[None]):
    """Renders the event stream. Takes an engine and nothing else."""

    TITLE = "simulacra"

    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #prose { width: 1fr; }
    #log { height: 1fr; padding: 0 1; }
    #live { height: auto; padding: 0 1; }
    #side { width: 34; min-width: 22; }
    #status { height: auto; border: round $panel; padding: 0 1; }
    #map { height: 1fr; border: round $panel; padding: 0 1; }
    #thinking { height: 1; padding: 0 1; color: $text 60%; }
    #cmd { border: none; padding: 0 1; height: 1; background: $surface; }
    """

    BINDINGS = [
        Binding("ctrl+c", "leave", "quit", priority=True, show=True),
        Binding("ctrl+l", "clear_log", "clear", priority=True, show=True),
        Binding("escape", "leave", "quit", show=False),
    ]

    def __init__(
        self,
        engine,
        *,
        observers: Iterable = (),
        on_end: Callable[[RunEnded], None] | None = None,
    ) -> None:
        super().__init__()
        self.engine = engine
        # Fanned out on the worker thread, exactly as `__main__` fans them out
        # on the main thread for the REPL. The store is thread-tolerant and the
        # engine is already using it from here.
        self.observers = list(observers)
        self.on_end = on_end
        self.ended = False
        self._prose = ""
        self._flush = None
        self._spinner = None
        self._spin_at = 0
        self._thinking_label = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="prose"):
                yield RichLog(id="log", wrap=True, markup=False, highlight=False,
                              auto_scroll=True)
                # The in-flight paragraph. RichLog renders each write to fixed
                # strips, so a partial line cannot be appended to -- which is
                # what streaming needs. Prose grows here and is committed to the
                # log on ProseEnd. Getting this wrong is what turns streamed
                # text into one word per line.
                yield Static("", id="live", markup=False)
            with Vertical(id="side"):
                yield StatusPanel(id="status")
                yield MapPanel(id="map")
        yield Static("", id="thinking", markup=False)
        yield CommandInput(placeholder="what do you do?", id="cmd")

    def on_mount(self) -> None:
        self.query_one("#cmd", CommandInput).disabled = True
        self.run_turn(None)

    # -- the worker --------------------------------------------------------

    @work(thread=True, exclusive=True, group="turn")
    def run_turn(self, text: str | None) -> None:
        """One turn, off the event loop. `None` means the opening beat.

        `exclusive` is a backstop, not the mechanism: a thread worker cannot
        actually be interrupted mid-generator. The disabled input is what
        genuinely stops turns queueing behind a 20 s director call, which is
        the honest representation of a single-threaded engine talking to a
        client that serializes requests anyway.
        """
        stream: Iterator[Event]
        stream = self.engine.begin() if text is None else self.engine.turn(text)
        try:
            for event in stream:
                for sink in self.observers:
                    sink.handle(event)
                self.call_from_thread(self.dispatch, event)
        except Exception as exc:                      # pragma: no cover - defensive
            self.call_from_thread(
                self.dispatch, Notice(f"the turn failed: {exc!r}", level="error")
            )
        finally:
            self.call_from_thread(self._turn_finished)

    def _turn_finished(self) -> None:
        self._clear_thinking()
        if self.ended:
            return
        cmd = self.query_one("#cmd", CommandInput)
        cmd.disabled = False
        cmd.focus()

    # -- rendering ---------------------------------------------------------

    def dispatch(self, event: Event) -> None:
        """The renderer proper. Unrecognised events are ignored on purpose --
        the same rule `repl.py` follows, and what lets a later milestone add an
        event without breaking this UI mid-flight."""
        # The engine says it has stopped thinking by continuing. Anything at
        # all arriving means the blocking call returned.
        if not isinstance(event, Thinking):
            self._clear_thinking()

        log = self.query_one("#log", RichLog)
        match event:
            case RoomEntered():
                log.write("")
                log.write(Text(event.name, style="bold"))
                if event.exits:
                    ordered = sorted(
                        event.exits,
                        key=lambda e: _EXIT_ORDER.index(e) if e in _EXIT_ORDER else 99,
                    )
                    shown = " ".join(_EXIT_SHORT.get(e, e) for e in ordered)
                    log.write(Text(f"exits: {shown}", style="dim"))
                self.query_one("#status", StatusPanel).entered(event)
                self.query_one("#map", MapPanel).entered(event)

            case Line():
                log.write(Text(event.text, style=_LINE_STYLES.get(event.style, "")))

            case ProseStart():
                self._prose = f"{event.speaker}: " if event.speaker else ""
                self._paint_prose()

            case ProseDelta():
                self._prose += event.text
                if self._flush is None:
                    self._flush = self.set_timer(_FLUSH_INTERVAL, self._flush_prose)

            case ProseEnd():
                self._stop_flush()
                if self._prose:
                    log.write(Text(self._prose))
                self._prose = ""
                self._paint_prose()

            case ItemTaken():
                log.write(Text(f"Taken: {event.item}", style="green"))

            case FloorDescended():
                log.write("")
                log.write(Text(f"You descend to floor {event.depth}. {event.theme_name}",
                               style="bold"))
                if event.goal:
                    log.write(Text(event.goal, style="dim"))
                self.query_one("#status", StatusPanel).descended(event)
                self.query_one("#map", MapPanel).descended()

            case NpcPresent():
                tag = " (remembers you)" if event.remembers else ""
                log.write(Text(f"{event.name} is here.{tag}", style="cyan"))

            case Roll():
                mark = "hit" if event.success else "miss"
                detail = f" {event.detail}" if event.detail else ""
                log.write(Text(
                    f"[{event.label}: {event.total} vs {event.target} -> {mark}]{detail}",
                    style="dim",
                ))

            case Damage():
                verb = "take" if event.target == "you" else "takes"
                log.write(Text(
                    f"{event.target} {verb} {event.amount} ({event.hp_left} left)",
                    style="red",
                ))
                if event.target == "you":
                    self.query_one("#status", StatusPanel).hurt(event.hp_left)

            case Improvised():
                log.write(Text(event.reason, style="dim" if event.plausible else "yellow"))

            case StatusChanged():
                self.query_one("#status", StatusPanel).status(event)

            case Thinking():
                self._start_thinking(event.label)

            case Notice():
                log.write(Text(event.text, style=_NOTICE_STYLES.get(event.level, "dim")))

            case RunEnded():
                self._run_ended(event)

            case _:
                pass

    # -- streamed prose ----------------------------------------------------

    def _paint_prose(self) -> None:
        self.query_one("#live", Static).update(Text(self._prose))

    def _flush_prose(self) -> None:
        self._flush = None
        self._paint_prose()

    def _stop_flush(self) -> None:
        if self._flush is not None:
            self._flush.stop()
            self._flush = None

    # -- the thinking beat -------------------------------------------------

    def _start_thinking(self, label: str) -> None:
        self._thinking_label = label
        self._spin_at = 0
        self._paint_thinking()
        if self._spinner is None:
            self._spinner = self.set_interval(_SPINNER_INTERVAL, self._tick_spinner)

    def _tick_spinner(self) -> None:
        self._spin_at = (self._spin_at + 1) % len(_SPINNER_FRAMES)
        self._paint_thinking()

    def _paint_thinking(self) -> None:
        frame = _SPINNER_FRAMES[self._spin_at]
        self.query_one("#thinking", Static).update(
            Text(f"{frame} {self._thinking_label}...", style="italic")
        )

    def _clear_thinking(self) -> None:
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner = None
        if self._thinking_label:
            self._thinking_label = ""
            self.query_one("#thinking", Static).update("")

    @property
    def thinking(self) -> bool:
        return bool(self._thinking_label)

    # -- input -------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        cmd = self.query_one("#cmd", CommandInput)
        cmd.value = ""
        if not text or self.ended:
            return

        cmd.remember(text)
        # Echoed so the log reads as a conversation rather than a monologue.
        self.query_one("#log", RichLog).write(Text(f"> {text}", style="bold cyan"))
        cmd.disabled = True
        self.run_turn(text)

    def _run_ended(self, event: RunEnded) -> None:
        self.ended = True
        if self.on_end is not None:
            self.on_end(event)

        log = self.query_one("#log", RichLog)
        log.write("")
        log.write(Text(
            f"The run ends: {event.cause}. Floor {event.depth}, {event.turns} turns.",
            style="bold red",
        ))
        if event.epitaph:
            log.write(Text(event.epitaph, style="italic"))

        self.query_one("#cmd", CommandInput).disabled = True
        if event.cause == "quit":
            self.exit()
            return
        # A death is worth reading. Hold the screen rather than dropping the
        # player back to a shell mid-epitaph.
        self.query_one("#thinking", Static).update(
            Text("the run has ended -- escape to leave", style="dim")
        )

    # -- actions -----------------------------------------------------------

    def action_leave(self) -> None:
        self.exit()

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()


def play_tui(session) -> None:
    """Entry point from `__main__`. Note what is *not* passed: `session.state`.

    Memory rides along as an observer of the same stream, exactly as it does in
    the REPL loop -- it is not a dependency of either frontend.
    """
    SimulacraApp(
        session.engine,
        observers=(session.memory,),
        on_end=session.note_end,
    ).run()
