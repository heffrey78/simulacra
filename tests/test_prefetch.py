"""Prefetcher: the M2 latency thesis, tested without a model.

Two properties matter and neither is obvious from reading the code: work is
strictly serial (parallel generations on CPU make each other slower), and the
foreground can take the model back promptly rather than waiting out an in-flight
generation.
"""

from __future__ import annotations

import random
import threading
import time

import pytest

from simulacra.config import Settings
from simulacra.memory.store import Store
from simulacra.narrate.narrator import Narrator
from simulacra.narrate.prefetch import Prefetcher
from simulacra.world.floorgen import generate_floor

from conftest import FakeClient

GOOD = "Silt has drifted into the corners. The air tastes of old iron."


@pytest.fixture
def floor(theme):
    return generate_floor(3, theme, random.Random(7))


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "p.db")
    yield s
    s.close()


def build(theme, store, *, delay=0.0, enabled=True):
    client = FakeClient(script=[GOOD], delay=delay)
    narrator = Narrator(client, theme, store, Settings().narrator)
    pf = Prefetcher(narrator, store, enabled=enabled)
    return client, narrator, pf


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_scheduled_rooms_get_narrated_in_the_background(theme, store, floor):
    client, narrator, pf = build(theme, store)
    pf.start()
    try:
        targets = list(floor.rooms)[:3]
        pf.schedule(floor, targets)
        assert wait_until(lambda: pf.stats.completed >= 3), pf.stats
        for rid in targets:
            assert store.cached_prose(narrator._key(floor, floor.room(rid))) == GOOD
    finally:
        pf.stop()


def test_arrival_after_prefetch_is_a_cache_hit(theme, store, floor):
    client, narrator, pf = build(theme, store)
    pf.start()
    try:
        room = floor.room(floor.entrance_id)
        pf.schedule(floor, [room.id])
        assert wait_until(lambda: pf.stats.completed >= 1)

        before = client.stream_calls
        assert pf.has_prose(floor, room) is True
        assert "".join(narrator.room(floor, room)) == GOOD
        assert client.stream_calls == before, "cache hit still called the model"
        assert pf.stats.hits == 1 and pf.stats.misses == 0
    finally:
        pf.stop()


def test_arrival_before_prefetch_is_recorded_as_a_miss(theme, store, floor):
    _, _, pf = build(theme, store)
    room = floor.room(floor.entrance_id)
    assert pf.has_prose(floor, room) is False
    assert pf.stats.misses == 1


def test_preempt_abandons_work_in_flight(theme, store, floor):
    """The whole point: a foreground turn must not wait out a generation."""
    client, _, pf = build(theme, store, delay=0.02)
    pf.start()
    try:
        pf.schedule(floor, list(floor.rooms))
        assert wait_until(lambda: client.stream_calls >= 1)
        pf.preempt()
        assert wait_until(lambda: client.closed_early >= 1, timeout=3.0), \
            "in-flight prefetch was not abandoned"
    finally:
        pf.stop()


def test_preemption_does_not_spin(theme, store, floor):
    """Regression: the first live M2 run logged 3.7M 'abandoned' in four turns.

    `Event.wait()` returns immediately when the event is *set*, so waiting on the
    cancel flag to mean 'wait until preemption ends' busy-looped a whole core --
    which on a CPU-inference box is stolen directly from the model.
    """
    _, _, pf = build(theme, store, delay=0.005)
    pf.start()
    try:
        pf.schedule(floor, list(floor.rooms))
        pf.preempt()
        time.sleep(0.3)
        churn = pf.stats.completed + pf.stats.abandoned
        assert churn < 50, f"worker span while preempted: {churn} items in 0.3s"
    finally:
        pf.stop()


def test_work_resumes_after_preemption_is_lifted(theme, store, floor):
    client, narrator, pf = build(theme, store)
    pf.start()
    try:
        pf.preempt()
        pf.schedule(floor, [floor.entrance_id])  # schedule lifts the preemption
        assert wait_until(lambda: pf.stats.completed >= 1), pf.stats
    finally:
        pf.stop()


def test_worker_never_runs_two_generations_at_once(theme, store, floor):
    """Serial by construction -- concurrency here would slow the foreground."""
    concurrent = 0
    peak = 0
    lock = threading.Lock()

    class CountingClient(FakeClient):
        def stream(self, messages, policy, *, kind="stream"):
            nonlocal concurrent, peak
            with lock:
                concurrent += 1
                peak = max(peak, concurrent)
            try:
                yield from super().stream(messages, policy, kind=kind)
            finally:
                with lock:
                    concurrent -= 1

    client = CountingClient(script=[GOOD], delay=0.005)
    narrator = Narrator(client, theme, store, Settings().narrator)
    pf = Prefetcher(narrator, store)
    pf.start()
    try:
        pf.schedule(floor, list(floor.rooms))
        assert wait_until(lambda: pf.stats.completed + pf.stats.abandoned >= 4)
        assert peak == 1, f"ran {peak} generations concurrently"
    finally:
        pf.stop()


def test_already_cached_rooms_are_not_regenerated(theme, store, floor):
    client, narrator, pf = build(theme, store)
    room = floor.room(floor.entrance_id)
    store.cache_prose(narrator._key(floor, room), "already here")

    pf.start()
    try:
        pf.schedule(floor, [room.id])
        # Skipped, not "completed" -- `completed` must track real model calls.
        assert wait_until(lambda: pf.stats.scheduled >= 1)
        time.sleep(0.2)
        assert client.stream_calls == 0
        assert pf.stats.completed == 0
    finally:
        pf.stop()


def test_disabled_prefetcher_does_nothing(theme, store, floor):
    client, _, pf = build(theme, store, enabled=False)
    pf.start()
    pf.schedule(floor, list(floor.rooms))
    time.sleep(0.05)
    assert client.stream_calls == 0
    assert pf.stats.scheduled == 0
    pf.stop()


def test_a_failing_prefetch_does_not_kill_the_worker(theme, store, floor):
    class Boom(FakeClient):
        def stream(self, messages, policy, *, kind="stream"):
            raise RuntimeError("model exploded")
            yield  # pragma: no cover

    narrator = Narrator(Boom(), theme, store, Settings().narrator)
    pf = Prefetcher(narrator, store)
    pf.start()
    try:
        pf.schedule(floor, list(floor.rooms)[:2])
        assert wait_until(lambda: pf.stats.abandoned >= 2), pf.stats
        assert pf._thread is not None and pf._thread.is_alive()
    finally:
        pf.stop()


def test_stop_joins_the_worker(theme, store, floor):
    _, _, pf = build(theme, store)
    pf.start()
    thread = pf._thread
    pf.schedule(floor, list(floor.rooms))
    pf.stop()
    assert not thread.is_alive()
