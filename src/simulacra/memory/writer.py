"""The write path: an observer, not a dependency of the turn loop.

The engine emits `Transcript` events; this consumes them. It implements the same
shape as a `Renderer`, so `__main__` simply fans events out to both:

    for event in engine.turn(text):
        renderer.handle(event)
        memory.handle(event)

**Reads are not symmetric with writes, and that is deliberate.** Recall has to
happen at an exact point in a turn -- before NPC dialogue is generated -- so the
engine queries the store directly. Writes are allowed to happen late, so they go
through events and never block the player.

Embedding runs on a background thread for the same reason the prefetcher does:
it is a model call, and model calls do not belong on the turn loop. The memory
row is written immediately and the vector is attached whenever it arrives.

MILESTONE M4.
"""

from __future__ import annotations

import queue
import threading

from ..engine.events import Event, RunEnded, Transcript


class MemoryWriter:
    def __init__(self, store, state, *, client=None, embed_policy=None, background: bool = True):
        self._store = store
        self._state = state
        self._client = client
        self._policy = embed_policy
        self._q: queue.Queue = queue.Queue()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self.written = 0
        self.embedded = 0
        self.embed_failures = 0

        if background and client is not None:
            self._thread = threading.Thread(
                target=self._run, name="memory-embed", daemon=True
            )
            self._thread.start()

    # -- observer ----------------------------------------------------------

    def handle(self, event: Event) -> None:
        if isinstance(event, Transcript):
            self._record(event)
        elif isinstance(event, RunEnded):
            # Flush before the process can exit out from under the worker.
            self.close()

    def _record(self, t: Transcript) -> None:
        memory_id = self._store.remember(
            self._state.run_id,
            t.summary,
            kind=t.kind,
            subjects=t.subjects,
            tags=t.tags,
            turn=self._state.turns,
            depth=self._state.depth,
        )
        self.written += 1

        if self._client is None:
            return
        if self._thread is not None:
            self._q.put((memory_id, t.summary))
        else:
            # Synchronous mode: tests, and anything that needs determinism.
            self._embed(memory_id, t.summary)

    # -- embedding ---------------------------------------------------------

    def _embed(self, memory_id: int, text: str) -> None:
        try:
            vectors = self._client.embed([text], self._policy)
            self._store.attach_embedding(memory_id, vectors[0])
            self.embedded += 1
        except Exception:
            # The row survives without a vector and stays graph-recallable.
            self.embed_failures += 1

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            self._embed(*item)

    def close(self) -> None:
        if self._thread is None or self._stopped.is_set():
            return
        # Drain rather than drop: an unembedded memory is a worse outcome than
        # a short wait on the way out.
        self._stopped.set()
        self._q.put(None)
        self._thread.join(timeout=5.0)
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                self._embed(*item)
        self._thread = None
