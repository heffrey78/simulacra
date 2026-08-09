"""Generate-ahead worker. The single largest perceived-latency win in the game.

The moment the player enters a room, we know the only places they can go next:
its neighbours. A background worker narrates those while the player is still
reading the current room's prose. By the time they type `north`, it's cached and
appears instantly.

Two rules that make this work rather than backfire:

1. **One worker thread, strictly serial.** Ollama accepts concurrent requests,
   but on CPU they share cores -- two parallel generations each run at half
   speed and the foreground turn gets slower, not faster. The queue exists to
   order work, not to parallelise it.
2. **Foreground preempts.** If the player moves somewhere we haven't finished,
   the in-flight prefetch is abandoned and the foreground request goes first.
   A prefetch that delays a real turn is worse than no prefetch.

Rule 2 is why the narrator streams even when it only wants a whole string:
`OllamaClient` serializes requests behind a lock, so a prefetch that ran to
completion would block the foreground for a full generation no matter how
promptly we asked it to stop. Streaming lets the worker drop out between tokens
and release the lock in milliseconds.

MILESTONE M2.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field


@dataclass
class PrefetchStats:
    """Is the generate-ahead actually landing? The M2 go/no-go depends on it."""

    scheduled: int = 0
    completed: int = 0
    abandoned: int = 0
    hits: int = 0     # room already had prose when the player arrived
    misses: int = 0   # player outran the worker
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def bump(self, name: str) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + 1)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def summary(self) -> str:
        return (
            f"prefetch: {self.hits}/{self.hits + self.misses} hits "
            f"({self.hit_rate * 100:.0f}%), {self.completed} generated, "
            f"{self.abandoned} abandoned"
        )


class Prefetcher:
    def __init__(self, narrator, store, *, enabled: bool = True):
        self._narrator = narrator
        self._store = store
        self._enabled = enabled
        self._q: queue.Queue = queue.Queue()
        # Two flags, because they answer different questions. `_cancel` aborts a
        # generation already in flight; `_resume` gates whether the worker may
        # start a new one. Collapsing them into one Event produced a hot spin:
        # Event.wait() returns immediately when the event is *set*, so "wait
        # until preemption is over" cannot be expressed with the cancel flag.
        self._cancel = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._queued: set[str] = set()
        self._lock = threading.Lock()
        self.stats = PrefetchStats()

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="prefetch", daemon=True)
        self._thread.start()

    def schedule(self, floor, room_ids) -> None:
        """Queue narration for rooms not already in the prose cache.

        Also clears the cancel flag: a call to schedule means the foreground is
        done with the model and the worker may resume.
        """
        if not self._enabled:
            return
        self._cancel.clear()
        self._resume.set()
        for room_id in room_ids:
            room = floor.rooms.get(room_id)
            if room is None:
                continue
            with self._lock:
                if room_id in self._queued:
                    continue
                self._queued.add(room_id)
            self.stats.bump("scheduled")
            self._q.put((floor, room))

    def preempt(self) -> None:
        """Abandon in-flight work; the player is waiting on something else.

        `schedule()` lifts it again, which the engine calls once the foreground
        narration is done.
        """
        self._resume.clear()
        self._cancel.set()

    def _cached(self, floor, room) -> bool:
        return self._store.cached_prose(self._narrator._key(floor, room)) is not None

    def has_prose(self, floor, room) -> bool:
        """Did the worker get there first? Records the hit/miss either way."""
        ready = self._cached(floor, room)
        self.stats.bump("hits" if ready else "misses")
        return ready

    def stop(self) -> None:
        self._stopped.set()
        self._cancel.set()
        self._resume.set()  # so a preempted worker can notice it should exit
        self._q.put(None)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- worker ------------------------------------------------------------

    def _run(self) -> None:
        while not self._stopped.is_set():
            # Block while the foreground owns the model. The timeout is only so
            # that `stop()` is noticed promptly.
            if not self._resume.wait(timeout=0.2):
                continue

            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break

            floor, room = item
            with self._lock:
                self._queued.discard(room.id)

            if self._cancel.is_set():
                # Preempted between the wait and the get. Put it back rather
                # than dropping it -- the player is probably heading there --
                # and do not count it as work, or the stats become nonsense.
                with self._lock:
                    self._queued.add(room.id)
                self._q.put((floor, room))
                continue

            if self._cached(floor, room):
                # Already narrated -- by an earlier visit, or a queued duplicate.
                # Counting it as generated inflates `completed` past the number
                # of model calls actually made.
                continue

            try:
                text = self._narrator.prepare(floor, room, cancel=self._cancel)
            except Exception:
                # A failed prefetch must never take the game down; the room will
                # simply be narrated live on arrival.
                text = None

            self.stats.bump("completed" if text else "abandoned")
