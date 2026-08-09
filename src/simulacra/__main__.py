"""Entry point.

The game runs with or without a model. If Ollama is unreachable, `--offline` (or
a failed health check) drops to M1 behaviour -- procedural names, no prose --
rather than refusing to start. That keeps the whole M1 test suite meaningful and
means a stopped daemon is a degraded game, not a broken one.

Startup order matters: warm the model *before* the title screen so the 8-31s
cold load overlaps with the player reading the intro rather than stalling turn
one.

M5 adds `--ui tui`. The frontend choice is the *last* decision made here:
everything above it -- settings, theme, store, client warmup, `new_run`,
`Engine`, `MemoryWriter` -- is identical whichever renderer consumes the events.
That is the seam being cashed in, so it is expressed as one `Session` both
branches build rather than two setup paths that drift.

MILESTONE M2 -> M5.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings
from .engine.events import RunEnded
from .engine.loop import Engine
from .engine.state import new_run
from .llm.client import OllamaClient
from .memory.store import Store
from .memory.writer import MemoryWriter
from .narrate.narrator import Narrator
from .narrate.prefetch import Prefetcher
from .world.theme import Theme


class Session:
    """A built, playable run: engine, memory observer, and the teardown they
    share. Frontends consume `engine`; they never assemble one."""

    def __init__(self, settings, theme, store, state, engine, memory, client, prefetcher):
        self.settings = settings
        self.theme = theme
        self.store = store
        self.state = state
        self.engine = engine
        self.memory = memory
        self.client = client
        self.prefetcher = prefetcher
        # A frontend sets these when it sees RunEnded. "abandoned" is the honest
        # default: a closed window is not a death.
        self.cause = "abandoned"
        self.epitaph = ""

    def note_end(self, event: RunEnded) -> None:
        self.cause, self.epitaph = event.cause, event.epitaph

    def close(self, *, stats: bool = False) -> None:
        """Always runs. An orphaned open row confuses previous_runs()."""
        if self.prefetcher is not None:
            self.prefetcher.stop()
        self.memory.close()
        self.store.end_run(
            self.state.run_id, cause=self.cause, depth=self.state.depth,
            turns=self.state.turns, epitaph=self.epitaph,
        )
        self.store.close()
        if self.client is not None:
            if stats:
                print("\n--- timings ---", file=sys.stderr)
                for kind, s in self.client.stats().items():
                    print(f"  {kind:<12} {s}", file=sys.stderr)
                if self.prefetcher is not None:
                    print(f"  {self.prefetcher.stats.summary()}", file=sys.stderr)
                print(f"  memories     {self.memory.written} written, "
                      f"{self.memory.embedded} embedded, "
                      f"{self.memory.embed_failures} failed", file=sys.stderr)
            self.client.close()


def build_session(args, settings: Settings, theme: Theme) -> Session:
    client = narrator = prefetcher = None
    if not args.offline:
        client = OllamaClient(settings)
        if args.model:
            settings = Settings(**{**settings.__dict__,
                                   "chat": settings.chat.with_(name=args.model)})
        ok, msg = client.health()
        if not ok:
            print(f"[no model: {msg}]\n[running offline -- procedural names only]",
                  file=sys.stderr)
            client.close()
            client = None
        else:
            # Before the title screen, so the cold load overlaps the intro.
            client.warm(settings.chat)

    store = Store(settings.db_path)
    if client is not None:
        narrator = Narrator(client, theme, store, settings.narrator)
        prefetcher = Prefetcher(narrator, store, enabled=not args.no_prefetch)

    state = new_run(store, theme, settings, seed=args.seed)
    engine = Engine(
        state, store, settings, theme,
        narrator=narrator, prefetcher=prefetcher, client=client,
    )
    # Memory is an observer of the same event stream, not a dependency of the
    # turn loop. See memory/writer.py for why reads are not symmetric.
    memory = MemoryWriter(store, state, client=client, embed_policy=settings.embed)
    return Session(settings, theme, store, state, engine, memory, client, prefetcher)


def play_repl(session: Session) -> None:
    """The M1 loop, unchanged: read a line, fan every event out to renderer and
    memory. Scriptable and pipeable, which is exactly why it stays."""
    from .ui.repl import ReplRenderer, read_input

    renderer = ReplRenderer()
    engine, memory, state = session.engine, session.memory, session.state

    for event in engine.begin():
        renderer.handle(event)
        memory.handle(event)

    while state.player.alive:
        text = read_input()
        ended = False
        for event in engine.turn(text):
            renderer.handle(event)
            memory.handle(event)
            if isinstance(event, RunEnded):
                session.note_end(event)
                ended = True
        if ended:
            break


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="simulacra")
    ap.add_argument("--ui", choices=["repl", "tui"], default="repl",
                    help="frontend (tui needs the 'tui' extra)")
    ap.add_argument("--theme", help="theme pack name (see themes/)")
    ap.add_argument("--model", help="override the chat model")
    ap.add_argument("--db", help="path to the world database")
    ap.add_argument("--seed", type=int, help="deterministic floor generation")
    ap.add_argument("--no-prefetch", action="store_true", help="disable generate-ahead")
    ap.add_argument("--offline", action="store_true", help="no model: procedural names only")
    ap.add_argument("--stats", action="store_true", help="print LLM timings on exit")
    ap.add_argument("--bench", action="store_true", help="run the latency harness and exit")
    args = ap.parse_args(argv)

    if args.bench:
        from .llm.doctor import main as doctor_main
        return doctor_main([])

    if args.ui == "tui":
        # Imported before anything is built, so a missing extra costs nothing
        # and says what to do about it.
        try:
            from .ui.tui import play_tui
        except ImportError as e:  # pragma: no cover - depends on install shape
            print(f"[the tui frontend needs Textual: {e}]\n"
                  "[install with: pip install -e '.[tui]']", file=sys.stderr)
            return 1
    else:
        play_tui = None

    settings = Settings.load()
    if args.db:
        settings = Settings(**{**settings.__dict__, "db_path": Path(args.db)})

    try:
        theme = Theme.load(args.theme or settings.theme)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1

    session = build_session(args, settings, theme)
    try:
        if play_tui is not None:
            play_tui(session)
        else:
            play_repl(session)
    finally:
        session.close(stats=args.stats)

    print(f"\nseed {session.state.seed} -- replay with: simulacra --seed {session.state.seed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
