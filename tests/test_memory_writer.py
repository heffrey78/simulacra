"""The write path. Memory is an observer; nothing here may block a turn."""

from __future__ import annotations

import time

import pytest

from simulacra.config import Settings
from simulacra.engine.events import Line, RunEnded, Transcript
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.memory.writer import MemoryWriter
from simulacra.world.theme import Theme

from conftest import FakeClient


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "w.db")
    yield s
    s.close()


@pytest.fixture
def state(store, theme):
    return new_run(store, theme, Settings(), seed=1)


def transcript(**kw):
    base = dict(summary="A delver died on floor 3.", kind="death",
                subjects=("npc:archivist",), tags=("death",))
    base.update(kw)
    return Transcript(**base)


def count(store) -> int:
    return store.db.execute("SELECT count(*) FROM memories").fetchone()[0]


def vectors(store) -> int:
    return store.db.execute("SELECT count(*) FROM memory_vectors").fetchone()[0]


# -- writing ---------------------------------------------------------------


def test_transcripts_become_memories(store, state):
    w = MemoryWriter(store, state, client=FakeClient(), background=False)
    w.handle(transcript())
    assert count(store) == 1 and w.written == 1


def test_other_events_are_ignored(store, state):
    w = MemoryWriter(store, state, client=FakeClient(), background=False)
    w.handle(Line(text="you walked north"))
    assert count(store) == 0


def test_subjects_are_what_make_recall_possible(store, state):
    w = MemoryWriter(store, state, client=FakeClient(), background=False)
    w.handle(transcript(subjects=("npc:archivist", "room:d1r0")))
    assert store.recall(about="npc:archivist", exclude_run=state.run_id + 1)
    assert store.recall(about="room:d1r0", exclude_run=state.run_id + 1)


def test_run_and_turn_are_taken_from_live_state(store, state):
    state.turns, state.depth = 17, 4
    w = MemoryWriter(store, state, client=FakeClient(), background=False)
    w.handle(transcript())
    row = store.db.execute("SELECT run_id, turn, depth FROM memories").fetchone()
    assert (row["run_id"], row["turn"], row["depth"]) == (state.run_id, 17, 4)


# -- embedding -------------------------------------------------------------


def test_memories_get_vectors(store, state):
    w = MemoryWriter(store, state, client=FakeClient(), background=False)
    w.handle(transcript())
    assert vectors(store) == 1 and w.embedded == 1


def test_a_failed_embedding_still_leaves_a_recallable_memory(store, state):
    """A memory with no vector is graph-recallable. A lost memory is lost."""

    class Boom(FakeClient):
        def embed(self, texts, policy=None):
            raise RuntimeError("embed model down")

    w = MemoryWriter(store, state, client=Boom(), background=False)
    w.handle(transcript())

    assert count(store) == 1
    assert vectors(store) == 0
    assert w.embed_failures == 1
    assert store.recall(about="npc:archivist", exclude_run=state.run_id + 1)


def test_a_wrong_sized_embedding_is_survived(store, state):
    class Short(FakeClient):
        def embed(self, texts, policy=None):
            return [[0.0] * 3]

    w = MemoryWriter(store, state, client=Short(), background=False)
    w.handle(transcript())
    assert count(store) == 1 and w.embed_failures == 1


def test_no_client_means_rows_but_no_vectors(store, state):
    w = MemoryWriter(store, state, client=None, background=False)
    w.handle(transcript())
    assert count(store) == 1 and vectors(store) == 0


# -- background ------------------------------------------------------------


def test_embedding_happens_off_the_caller_thread(store, state):
    """The turn loop must never wait on an embed call."""
    client = FakeClient(delay=0.05)
    w = MemoryWriter(store, state, client=client, background=True)
    try:
        t0 = time.perf_counter()
        for _ in range(5):
            w.handle(transcript())
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.1, f"handle() blocked for {elapsed:.2f}s"
        assert count(store) == 5, "rows must be written synchronously"
    finally:
        w.close()


def test_close_drains_outstanding_work(store, state):
    w = MemoryWriter(store, state, client=FakeClient(), background=True)
    for _ in range(6):
        w.handle(transcript())
    w.close()
    assert vectors(store) == 6, "embeddings were dropped on the way out"


def test_run_ended_flushes(store, state):
    w = MemoryWriter(store, state, client=FakeClient(), background=True)
    w.handle(transcript())
    w.handle(RunEnded(cause="a ghoul", depth=3, turns=40))
    assert vectors(store) == 1


def test_close_is_idempotent(store, state):
    w = MemoryWriter(store, state, client=FakeClient(), background=True)
    w.handle(transcript())
    w.close()
    w.close()
