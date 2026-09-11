"""The POC's acceptance criterion: run 2 knows what happened in run 1.

Assertions are on the **prompt sent to the model**, never on its output. An NPC
that says something vaguely ominous will feel like memory; the model will
happily improvise continuity it was never given. Only the prompt proves it.
"""

from __future__ import annotations

import pytest

from simulacra.config import Settings
from simulacra.engine.events import NpcPresent, RunEnded
from simulacra.engine.loop import Engine
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.memory.writer import MemoryWriter
from simulacra.narrate.narrator import Narrator
from simulacra.world.model import Actor, RoomKind

from conftest import FakeClient

ARCHIVIST = "npc:archivist"
REPLY = "Two hundred and nine went down. Two hundred and nine did not come back."


class Recording(FakeClient):
    """Captures every prompt so tests can assert on what the model was told."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.prompts: list[str] = []

    def stream(self, messages, policy, *, kind="stream"):
        self.prompts.append("\n".join(m["content"] for m in messages))
        yield from super().stream(messages, policy, kind=kind)

    def complete(self, messages, policy, *, kind="complete"):
        self.prompts.append("\n".join(m["content"] for m in messages))
        return super().complete(messages, policy, kind=kind)

    def embed(self, texts, policy=None):
        # Distinct-but-deterministic vectors, so KNN ordering is meaningful.
        return [[(hash(t) % 97) / 97.0] * 768 for t in texts]


def play(store, theme, *, seed, client, die=False, talk=True):
    """One run: meet the Archivist, optionally die, close the run."""
    settings = Settings()
    narrator = Narrator(client, theme, store, settings.narrator)
    state = new_run(store, theme, settings, seed=seed)
    engine = Engine(state, store, settings, theme,
                    narrator=narrator, client=client)
    writer = MemoryWriter(store, state, client=client, background=False)

    def run(gen):
        out = []
        for e in gen:
            writer.handle(e)
            out.append(e)
        return out

    events = run(engine.begin())

    if talk:
        # Stand where the Archivist is and speak to them.
        host = next(r for r in state.floor.rooms.values()
                    if any(a.id == ARCHIVIST for a in r.actors))
        state.room_id = host.id
        events += run(engine.turn("talk to archivist"))

    if die:
        state.depth = 3
        state.player.hp = 1
        state.room.actors.append(
            Actor(id="m", name="a smudged figure", hp=99, max_hp=99,
                  attack=30, defense=99))
        for _ in range(25):
            batch = run(engine.turn("attack figure"))
            events += batch
            if any(isinstance(e, RunEnded) for e in batch):
                break

    ended = [e for e in events if isinstance(e, RunEnded)]
    store.end_run(state.run_id, cause=ended[0].cause if ended else "quit",
                  depth=state.depth, turns=state.turns)
    writer.close()
    return events


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "world.db")
    yield s
    s.close()


def test_the_archivist_remembers_the_previous_run(store, theme):
    """The milestone. Die in run 1; in run 2 the Archivist is told about it."""
    run1 = Recording(script=[REPLY])
    events1 = play(store, theme, seed=42, client=run1, die=True)

    died = [e for e in events1 if isinstance(e, RunEnded)]
    assert died and died[0].cause == "a smudged figure", "run 1 did not die as set up"

    # -- run 2, same store --------------------------------------------------
    run2 = Recording(script=[REPLY])
    events2 = play(store, theme, seed=7, client=run2)

    present = [e for e in events2 if isinstance(e, NpcPresent)]
    assert present, "the Archivist was never announced"
    assert any(e.remembers for e in present), "the Archivist recalled nothing"

    dialogue = [p for p in run2.prompts if "delver says" in p.lower()]
    assert dialogue, "no NPC dialogue prompt was ever built"
    prompt = dialogue[-1]
    assert "smudged figure" in prompt, f"the death was not in the prompt:\n{prompt}"
    assert "3" in prompt, "the floor was not in the prompt"


def test_the_first_ever_run_works_with_an_empty_store(store, theme):
    """Cold start: nothing to recall, and the NPC is still worth talking to."""
    client = Recording(script=[REPLY])
    events = play(store, theme, seed=42, client=client)

    # Talking re-announces an NPC only when a past run surfaces (M14.1), so on
    # a cold start there may be no announcement at all -- never a remembering one.
    present = [e for e in events if isinstance(e, NpcPresent)]
    assert not any(e.remembers for e in present)
    assert any("delver says" in p.lower() for p in client.prompts)


def test_a_memory_from_this_run_never_surfaces_in_this_run(store, theme):
    """Without exclude_run the NPC 'remembers' four turns ago as a past life."""
    client = Recording(script=[REPLY])
    play(store, theme, seed=42, client=client, die=True)

    # Everything written above belongs to run 1; ask as run 1 and get nothing.
    run_id = store.previous_runs()[0]["id"]
    same = store.recall(about=ARCHIVIST, exclude_run=run_id, limit=5)
    assert same == []

    other = store.recall(about=ARCHIVIST, exclude_run=run_id + 1, limit=5)
    assert other, "the memories were not written at all"


def test_memories_are_selective_not_per_turn(store, theme):
    """Remembering every turn fills the store with 'you walked north'."""
    client = Recording(script=[REPLY])
    settings = Settings()
    narrator = Narrator(client, theme, store, settings.narrator)
    state = new_run(store, theme, settings, seed=42)
    engine = Engine(state, store, settings, theme, narrator=narrator, client=client)
    writer = MemoryWriter(store, state, client=client, background=False)

    for e in engine.begin():
        writer.handle(e)
    for _ in range(12):
        direction = next(iter(state.room.exits)).value
        for e in engine.turn(direction):
            writer.handle(e)
    writer.close()

    n = store.db.execute("SELECT count(*) FROM memories").fetchone()[0]
    assert n < 8, f"{n} memories from 13 turns of walking"


def test_the_npc_is_the_same_graph_node_across_runs(store, theme):
    """A generated id per run would silently make a new NPC every time and the
    whole milestone would quietly do nothing.

    Since M7 the roster is seeded at world creation, so the count is the roster
    size from the start -- what must not change is that a second run adds none."""
    counts = []
    for seed in (42, 7):
        client = Recording(script=[REPLY])
        play(store, theme, seed=seed, client=client)
        counts.append(
            store.db.execute("SELECT count(*) FROM nodes WHERE kind='npc'").fetchone()[0]
        )

    assert counts[0] == len(theme.npcs), f"{counts[0]} npc nodes for {len(theme.npcs)} roster entries"
    assert counts[0] == counts[1], f"a second run added {counts[1] - counts[0]} npc nodes"


def test_a_death_outranks_chatter_in_what_the_npc_is_told(store, theme):
    """Recall ordering matters: a small model attends hardest to the end of its
    prompt, and the death is the thing worth saying out loud."""
    play(store, theme, seed=42, client=Recording(script=[REPLY]), die=True)

    client = Recording(script=[REPLY])
    play(store, theme, seed=7, client=client)

    prompt = [p for p in client.prompts if "delver says" in p.lower()][-1]
    recalled = [ln for ln in prompt.splitlines() if ln.startswith("- ")]
    assert len(recalled) >= 2, "not enough memories to order"
    assert "killed by" in recalled[-1], f"the death was not last:\n{prompt}"
