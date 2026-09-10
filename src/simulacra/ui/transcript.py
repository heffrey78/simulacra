"""A run, written down.

The event seam's third renderer. It *is* the REPL renderer -- the same class,
the same formatting -- pointed at a plain-text file, so there is no second copy
of "how an event reads" to drift away from the first. Nothing wrote a transcript
before M11, in either frontend; the 2026-09-10 playtest had to be copied out of
a terminal by hand, and the TUI would not let it be copied at all.

Commands are not events -- the engine only ever sees a line after parsing it --
so frontends pass them in through `command()`.

MILESTONE M11.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from ..engine.events import Event
from .repl import ReplRenderer

WIDTH = 100


class TranscriptWriter:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")
        # color_system=None and force_terminal=False: plain text with no escape
        # codes, whatever the terminal the game is running in supports.
        console = Console(
            file=self._file, markup=False, highlight=False,
            color_system=None, force_terminal=False, width=WIDTH,
        )
        self._renderer = ReplRenderer(console)

    def command(self, text: str) -> None:
        if self._file.closed:
            return
        self._file.write(f"> {text}\n")
        self._file.flush()

    def handle(self, event: Event) -> None:
        if self._file.closed:
            return
        self._renderer.handle(event)
        # Flushed per event: a transcript is most wanted after a crash, and a
        # text file at reading speed is not a cost worth buffering against.
        self._file.flush()

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()
