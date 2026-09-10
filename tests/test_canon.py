"""M7: typed knowledge with provenance.

The design's whole safety property is one string column meaning what it says.
The two tests that actually enforce it are `test_add_canon_rejects_...` and
`test_the_refresh_never_reads_derived_canon` -- treat both as load-bearing
rather than as coverage.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from simulacra.config import Settings
from simulacra.engine.events import Notice
from simulacra.engine.loop import TOLD_CAP, Engine
from simulacra.engine.state import ensure_npcs, new_run, place_npcs
from simulacra.memory import canonist
from simulacra.memory.store import SCHEMA_VERSION, Store
from simulacra.narrate.narrator import Narrator
from simulacra.world.floorgen import generate_floor
from simulacra.world.model import Actor
from simulacra.world.theme import Npc, Theme

from conftest import FakeClient

REPLY = "Two hundred and nine went down."


class Recorder(FakeClient):
    """FakeClient that keeps every prompt it was handed."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.prompts: list[list[dict]] = []

    def stream(self, messages, policy, *, kind="stream"):
        self.prompts.append(list(messages))
        yield from super().stream(messages, policy, kind=kind)

    def structured(self, messages, schema, policy, *, kind="structured", retries=1):
        self.prompts.append(list(messages))
        return super().structured(messages, schema, policy, kind=kind, retries=retries)


def prompt_text(client) -> str:
    return "\n".join(m["content"] for m in client.prompts[-1])


# -- C1: the table ---------------------------------------------------------


@pytest.mark.parametrize("bad", ["observed", "generated", "derived ", "", "AUTHORED"])
def test_add_canon_rejects_anything_outside_the_vocabulary(tmp_path, bad):
    """`observed` lives in `memories`; `generated` is never persisted at all.
    A value that slips through misspelled is recall-eligible *and* invisible to
    retire_canon, so it could never be taken back."""
    store = Store(tmp_path / "w.db")
    with pytest.raises(ValueError):
        store.add_canon("npc:x", "something", bad)
    store.close()


def test_canon_comes_back_in_prompt_order(tmp_path):
    store = Store(tmp_path / "w.db")
    store.add_canon("npc:x", "hearsay", "told")
    store.add_canon("npc:x", "worked out", "derived")
    store.add_canon("npc:x", "seed", "authored")

    assert [r["text"] for r in store.canon("npc:x")] == ["seed", "worked out", "hearsay"]
    store.close()


def test_retired_canon_never_comes_back(tmp_path):
    store = Store(tmp_path / "w.db")
    store.add_canon("npc:x", "seed", "authored")
    store.add_canon("npc:x", "wrong", "derived")

    assert store.retire_canon("npc:x", provenance="derived") == 1
    assert [r["text"] for r in store.canon("npc:x")] == ["seed"]
    store.close()


def test_the_cap_retires_oldest_first(tmp_path):
    store = Store(tmp_path / "w.db")
    for i in range(5):
        store.add_canon("npc:x", f"fact {i}", "derived")

    store.retire_canon("npc:x", provenance="derived", keep_newest=2)
    assert [r["text"] for r in store.canon("npc:x")] == ["fact 3", "fact 4"]
    store.close()


def test_a_v1_world_migrates_forward_and_keeps_everything(tmp_path):
    path = tmp_path / "w.db"
    store = Store(path, theme="simulacra", world_seed=11)
    store.remember(1, "a delver died on floor 3", subjects=["npc:archivist"])
    store.commit()
    store.close()

    db = sqlite3.connect(path)
    db.execute("UPDATE world SET schema_version = 1")
    db.execute("DROP TABLE canon")
    db.commit()
    db.close()

    store = Store(path)
    assert store.world()["schema_version"] == SCHEMA_VERSION
    assert store.world()["world_seed"] == 11
    assert store.db.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 1
    store.add_canon("npc:archivist", "still works", "authored")
    store.close()


# -- C2/C3: NPCs are residents ---------------------------------------------


def test_the_roster_carries_authored_canon(theme):
    assert all(n.canon for n in theme.npcs), "an NPC with no authored canon has no self"
    assert len({n.depth for n in theme.npcs}) > 1


def test_npcs_exist_before_anyone_has_met_them(tmp_path, theme):
    store = Store(tmp_path / "w.db")
    ensure_npcs(store, theme)

    for npc in theme.npcs:
        assert store.node(npc.anchor) is not None
        assert [r["text"] for r in store.canon(npc.anchor, provenance="authored")] == list(npc.canon)
    store.close()


def test_seeding_twice_does_not_duplicate_canon(tmp_path, theme):
    store = Store(tmp_path / "w.db")
    ensure_npcs(store, theme)
    ensure_npcs(store, theme)

    anchor = theme.npcs[0].anchor
    assert len(store.canon(anchor, provenance="authored")) == len(theme.npcs[0].canon)
    store.close()


def test_a_new_authored_line_reaches_an_existing_world(tmp_path, theme):
    """The theme pack is the source of truth for authored canon: adding a line
    to the roster should reach worlds that already exist."""
    store = Store(tmp_path / "w.db")
    ensure_npcs(store, theme)

    grown = Theme.load("simulacra")
    first = grown.npcs[0]
    grown.npcs[0] = Npc(anchor=first.anchor, name=first.name, role=first.role,
                        voice=first.voice, depth=first.depth,
                        canon=(*first.canon, "Has started leaving the door open."))
    ensure_npcs(store, grown)

    texts = [r["text"] for r in store.canon(first.anchor, provenance="authored")]
    assert "Has started leaving the door open." in texts
    assert len(texts) == len(first.canon) + 1
    store.close()


def test_reseeding_never_clobbers_where_an_npc_has_moved(tmp_path, theme):
    """`data` is written only on creation. Rewriting it would put a wandering
    NPC back in its generated room every single run."""
    store = Store(tmp_path / "w.db")
    ensure_npcs(store, theme)

    npc = theme.npcs[0]
    node = store.node(npc.anchor)
    data = json.loads(node["data"])
    data["room_id"] = "d1r0"
    store.upsert_node(npc.anchor, "npc", npc.name, data)
    store.commit()

    ensure_npcs(store, theme)
    assert json.loads(store.node(npc.anchor)["data"])["room_id"] == "d1r0"
    store.close()


def test_placement_is_written_back_the_first_time(tmp_path, theme):
    import random

    store = Store(tmp_path / "w.db")
    ensure_npcs(store, theme)
    npc = theme.npcs[0]
    floor = generate_floor(npc.depth, theme, random.Random(1))
    place_npcs(store, floor, theme)

    stored = json.loads(store.node(npc.anchor)["data"])["room_id"]
    assert stored is not None
    assert any(a.id == npc.anchor for a in floor.rooms[stored].actors)
    store.close()


def test_a_stored_room_wins_over_the_generated_one(tmp_path, theme):
    """The inversion C3 exists for: floorgen proposes, the world disposes. This
    is what makes M9's NPC movement a state update, not a floorgen rewrite."""
    import random

    store = Store(tmp_path / "w.db")
    ensure_npcs(store, theme)
    npc = theme.npcs[0]
    floor = generate_floor(npc.depth, theme, random.Random(1))

    generated = next(r.id for r in floor.rooms.values()
                     if any(a.id == npc.anchor for a in r.actors))
    elsewhere = next(rid for rid in floor.rooms if rid != generated)

    data = json.loads(store.node(npc.anchor)["data"])
    data["room_id"] = elsewhere
    store.upsert_node(npc.anchor, "npc", npc.name, data)
    store.commit()

    floor = generate_floor(npc.depth, theme, random.Random(1))
    place_npcs(store, floor, theme)

    assert any(a.id == npc.anchor for a in floor.rooms[elsewhere].actors)
    assert not any(a.id == npc.anchor for a in floor.rooms[generated].actors)
    store.close()


# -- C3: who is being addressed --------------------------------------------


@pytest.fixture
def two_npcs(tmp_path, theme):
    """Both NPCs in one room. The roster keeps them on different floors, so this
    constraint has never been exercised end to end -- and `_talk`'s old
    `npcs[0]` fallback was only correct while it held."""
    settings = Settings()
    store = Store(tmp_path / "w.db")
    client = Recorder(script=[REPLY])
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator),
                    client=client)
    list(engine.begin())

    host = next(r for r in state.floor.rooms.values()
                if any(not a.hostile for a in r.actors))
    state.room_id = host.id
    other = theme.npcs[1]
    host.actors.append(Actor(id=other.anchor, name=other.name, hp=1, max_hp=1,
                             attack=0, defense=99, hostile=False, archetype="npc"))
    yield engine, state, store, client
    store.close()


def test_an_unaddressed_greeting_asks_instead_of_guessing(two_npcs):
    engine, _, _, client = two_npcs
    before = client.stream_calls  # the fixture already narrated a room
    events = list(engine.turn("talk"))
    assert any(isinstance(e, Notice) for e in events)
    assert client.stream_calls == before, "it guessed and spent a model call on it"


def test_naming_one_of_them_reaches_that_one(two_npcs):
    engine, state, store, client = two_npcs
    list(engine.turn("talk to corrector"))
    assert "npc:corrector" in state.met_npcs
    assert "npc:archivist" not in state.met_npcs


def test_a_topic_after_the_name_still_resolves_the_name(two_npcs):
    """`ask archivist about the arm` used to match nobody -- the old test
    compared the whole remaining string against the name -- and fell through to
    `npcs[0]`, which anchors the wrong NPC's canon silently."""
    engine, state, _, _ = two_npcs
    list(engine.turn("ask corrector about the seam"))
    assert state.met_npcs == {"npc:corrector"}


# -- C6: canon in the prompt -----------------------------------------------


@pytest.fixture
def talking(tmp_path, theme):
    settings = Settings()
    store = Store(tmp_path / "w.db")
    client = Recorder(script=[REPLY])
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator),
                    client=client)
    list(engine.begin())
    host = next(r for r in state.floor.rooms.values()
                if any(not a.hostile for a in r.actors))
    state.room_id = host.id
    yield engine, state, store, client, theme.npcs[0]
    store.close()


def test_authored_canon_reaches_the_prompt(talking):
    engine, _, _, client, npc = talking
    list(engine.turn("talk to archivist"))
    text = prompt_text(client)
    assert "What is true of you:" in text
    assert npc.canon[0] in text


def test_canon_and_recollections_are_separate_sections(talking):
    """Merging them is how M4's prompt ended up instructing an NPC to recite a
    death regardless of what it was asked."""
    engine, state, store, client, _ = talking
    store.remember(state.run_id - 1, "A delver was killed by a duplicate on floor 2.",
                   kind="death", subjects=["npc:archivist"], embedding=[0.0] * 768)
    store.commit()

    list(engine.turn("talk to archivist"))
    text = prompt_text(client)
    assert text.index("What is true of you:") < text.index("You remember, from earlier delvers:")


def test_an_npc_with_canon_is_never_told_it_remembers_nothing(talking):
    engine, _, _, client, _ = talking
    list(engine.turn("talk to archivist"))
    assert "You remember nothing about this delver" not in prompt_text(client)


def test_every_branch_carries_an_instruction(talking):
    """A prompt that is nothing but data and the player's line makes a 1.7b
    transcribe the last thing it was handed. Measured live: an NPC with canon
    and no recollections said "They say nothing." once the guard caught it."""
    engine, state, store, client, npc = talking
    list(engine.turn("talk to archivist"))
    assert "Answer them from what is true of you." in prompt_text(client)

    store.remember(state.run_id - 1, "A delver was killed by a duplicate on floor 2.",
                   kind="death", subjects=[npc.anchor], embedding=[0.0] * 768)
    store.commit()
    list(engine.turn("talk to archivist"))
    assert "Say out loud what happened to the delver" in prompt_text(client)


def test_a_transcribed_prompt_never_reaches_the_player(talking):
    """The dialogue prompt has labels of its own, and the census list did not
    cover them -- measured live as "The delver said: archivist vault on floor
    two is already open." reaching the screen."""
    from simulacra.narrate.narrator import looks_like_echo

    engine, _, _, client, _ = talking
    list(engine.turn("talk to archivist"))
    prompt = prompt_text(client)
    assert looks_like_echo("The delver said: archivist vault is already open.", prompt)
    assert looks_like_echo("What is true of you: I keep the ledger.", prompt)
    assert not looks_like_echo("Two hundred and nine went down, and I counted each.", prompt)


def test_an_npc_with_nothing_at_all_still_gets_the_cold_start_line(talking):
    engine, state, store, client, npc = talking
    store.retire_canon(npc.anchor)
    list(engine.turn("talk to archivist"))
    assert "You remember nothing about this delver" in prompt_text(client)


def test_the_prompt_stays_inside_the_tier_two_budget(talking):
    """Rough token count with every cap full. The failure arrives on the world
    where an NPC has accumulated the maximum of everything, not on a fresh one."""
    engine, state, store, client, npc = talking
    for i in range(6):
        store.add_canon(npc.anchor, f"A derived fact number {i} about the ledger.", "derived")
        store.add_canon(npc.anchor, f"A delver claimed something number {i}.", "told")
    for i in range(4):
        store.remember(state.run_id - 1, f"A delver was killed by a duplicate on floor {i}.",
                       kind="death", subjects=[npc.anchor], embedding=[0.0] * 768)
    store.commit()

    list(engine.turn("ask archivist about the ledger"))
    words = len(prompt_text(client).split())
    assert words < 450, f"{words} words is past the ~600-token tier-2 budget"


# -- C4: hearsay -----------------------------------------------------------


def test_telling_an_npc_something_records_it_as_hearsay(talking):
    engine, state, store, _, npc = talking
    list(engine.turn("tell archivist the arm was still moving"))

    told = store.canon(npc.anchor, provenance="told")
    assert len(told) == 1
    assert "arm was still moving" in told[0]["text"]
    assert told[0]["confidence"] < 1.0
    assert told[0]["source_run"] == state.run_id


def test_hearsay_is_labelled_as_hearsay_in_the_prompt(talking):
    engine, _, store, client, npc = talking
    store.add_canon(npc.anchor, "the vault is empty", "told", confidence=0.5)
    list(engine.turn("talk to archivist"))
    text = prompt_text(client)
    assert "may have been lying" in text
    assert "say who told you" in text


def test_hearsay_is_capped(talking):
    engine, _, store, _, npc = talking
    for i in range(TOLD_CAP + 3):
        list(engine.turn(f"tell archivist claim number {i}"))
    assert len(store.canon(npc.anchor, provenance="told")) == TOLD_CAP


def test_telling_nobody_in_particular_asks(two_npcs):
    engine, _, store, _ = two_npcs
    assert any(isinstance(e, Notice) for e in engine.turn("tell them something"))
    assert store.canon("npc:archivist", provenance="told") == []


def test_telling_an_npc_nothing_is_refused(talking):
    engine, _, store, _, npc = talking
    assert any(isinstance(e, Notice) for e in engine.turn("tell archivist"))
    assert store.canon(npc.anchor, provenance="told") == []


def test_the_npc_still_never_records_its_own_words(talking):
    """M4's bug, re-checked now that a write path from conversation exists."""
    engine, _, store, _, npc = talking
    list(engine.turn("tell archivist the vault is open"))
    everything = [r["text"] for r in store.canon(npc.anchor)]
    everything += [r["text"] for r in store.db.execute("SELECT text FROM memories")]
    assert not any(REPLY in t for t in everything)


# -- C5: derived canon -----------------------------------------------------


@pytest.fixture
def ready(tmp_path, theme):
    store = Store(tmp_path / "w.db")
    ensure_npcs(store, theme)
    npc = theme.npcs[0]
    store.remember(1, "A delver was killed by a duplicate on floor 2.",
                   kind="death", subjects=[npc.anchor])
    store.commit()
    yield store, theme, npc
    store.close()


def test_the_refresh_never_reads_derived_canon(ready):
    """**The depth-1 rule.** Generating canon from canon compounds error across
    a world's lifetime; this is the guard that keeps model output a leaf."""
    store, theme, npc = ready
    store.add_canon(npc.anchor, "A PREVIOUSLY DERIVED CLAIM", "derived")
    store.commit()

    client = Recorder(structured_result={"facts": ["Counts every delver in tens."]})
    canonist.refresh(store, theme, client, Settings().director, run_id=2)

    assert "A PREVIOUSLY DERIVED CLAIM" not in prompt_text(client)
    assert npc.canon[0] in prompt_text(client), "authored canon should be an input"


def test_the_refresh_writes_derived_canon(ready):
    store, theme, npc = ready
    client = Recorder(structured_result={"facts": ["Counts every delver in tens.", "Has never once been seen asleep."]})
    written = canonist.refresh(store, theme, client, Settings().director, run_id=2)

    assert set(written) == {"Counts every delver in tens.", "Has never once been seen asleep."}
    texts = [r["text"] for r in store.canon(npc.anchor, provenance="derived")]
    assert texts == ["Counts every delver in tens.", "Has never once been seen asleep."]


def test_an_npc_with_no_new_memories_is_skipped(ready):
    """Without this every exit burns a tier-3 call regenerating identical canon."""
    store, theme, _ = ready
    policy = Settings().director

    canonist.refresh(store, theme, Recorder(structured_result={"facts": ["Keeps the ledger in a locked case."]}),
                     policy, run_id=2)
    second = Recorder(structured_result={"facts": ["Has begun numbering the doors as well."]})
    assert canonist.refresh(store, theme, second, policy, run_id=3) == []
    assert second.prompts == []


def test_a_new_memory_reopens_the_gate(ready):
    store, theme, npc = ready
    policy = Settings().director
    canonist.refresh(store, theme, Recorder(structured_result={"facts": ["Keeps the ledger in a locked case."]}),
                     policy, run_id=2)

    store.remember(3, "Another delver died on floor 1.", kind="death", subjects=[npc.anchor])
    store.commit()
    grown = canonist.refresh(store, theme, Recorder(structured_result={"facts": ["Has begun numbering the doors as well."]}),
                             policy, run_id=3)
    assert grown == ["Has begun numbering the doors as well."]


def test_a_repeated_fact_is_not_written_twice(ready):
    store, theme, npc = ready
    policy = Settings().director
    canonist.refresh(store, theme, Recorder(structured_result={"facts": ["Keeps the ledger in a locked case."]}),
                     policy, run_id=2)
    store.remember(3, "Another delver died.", kind="death", subjects=[npc.anchor])
    store.commit()
    assert canonist.refresh(store, theme, Recorder(structured_result={"facts": ["Keeps the ledger in a locked case."]}),
                            policy, run_id=3) == []


def test_derived_canon_is_capped(ready):
    store, theme, npc = ready
    policy = Settings().director
    for run in range(2, 8):
        store.remember(run, f"A delver died on floor {run}.", kind="death",
                       subjects=[npc.anchor])
        store.commit()
        canonist.refresh(store, theme,
                         Recorder(structured_result={"facts": [f"Has counted floor {run} more than once."]}),
                         policy, run_id=run)

    assert len(store.canon(npc.anchor, provenance="derived")) <= canonist.DERIVED_CAP


@pytest.mark.parametrize("bad", [
    "The Archivist's ledger is currently marked with 123456789.",  # measured, live
    "Short.",
    "the the the the the the the the the the",
    "A sentence so long it runs well past anything anyone would ever want to read "
    "aloud in a dungeon corridor and keeps going for no reason at all whatsoever.",
])
def test_degenerate_output_never_becomes_canon(ready, bad):
    """The narrator has had `looks_degenerate` since M4, but it guards streamed
    prose. Canon outlives the run: a bad line of narration scrolls away, a bad
    line of canon is in every conversation that NPC ever has again. The first
    case here came out of the live five-run read."""
    store, theme, npc = ready
    client = Recorder(structured_result={"facts": [bad]})
    assert canonist.refresh(store, theme, client, Settings().director, run_id=2) == []
    assert store.canon(npc.anchor, provenance="derived") == []


def test_a_good_fact_still_gets_through_alongside_a_bad_one(ready):
    store, theme, npc = ready
    client = Recorder(structured_result={"facts": [
        "The ledger is marked with 123456789.",
        "Keeps a second ledger nobody has seen.",
    ]})
    written = canonist.refresh(store, theme, client, Settings().director, run_id=2)
    assert written == ["Keeps a second ledger nobody has seen."]


def test_content_bearing_memories_outrank_chatter(ready):
    """A dialogue transcript records the shape of a question and not its answer.
    Filling the prompt with those is how canon says the same thing five runs
    running -- measured live before `prefer` existed."""
    store, theme, npc = ready
    for i in range(canonist.MEMORY_WINDOW + 2):
        store.remember(1, f"A delver approached and asked about thing {i}.",
                       kind="dialogue", subjects=[npc.anchor])
    store.commit()

    client = Recorder(structured_result={"facts": ["Keeps a second ledger nobody has seen."]})
    canonist.refresh(store, theme, client, Settings().director, run_id=2)
    assert "killed by a duplicate" in prompt_text(client), "the death was crowded out"


def test_a_failing_refresh_is_not_an_error(ready):
    """Same contract as the epitaph: a failed flourish must not break the exit."""
    store, theme, _ = ready
    client = Recorder(structured_result=RuntimeError("model went away"))
    assert canonist.refresh(store, theme, client, Settings().director, run_id=2) == []


def test_no_client_means_no_refresh(ready):
    store, theme, _ = ready
    assert canonist.refresh(store, theme, None, Settings().director, run_id=2) == []


def test_the_refresh_gives_up_rather_than_hanging(ready):
    """A player closing the game must close the game."""
    import time

    store, theme, _ = ready

    class Slow(FakeClient):
        def structured(self, *a, **kw):
            time.sleep(5)
            return {"facts": ["too late"]}

    t0 = time.perf_counter()
    out = canonist.refresh(store, theme, Slow(), Settings().director, run_id=2, timeout=0.2)
    assert time.perf_counter() - t0 < 2.0
    assert out == []


# -- C7: forget ------------------------------------------------------------


def test_forget_leaves_an_npc_with_its_authored_persona(tmp_path, theme):
    store = Store(tmp_path / "w.db")
    ensure_npcs(store, theme)
    npc = theme.npcs[0]
    store.add_canon(npc.anchor, "worked out", "derived")
    store.add_canon(npc.anchor, "hearsay", "told")
    store.remember(1, "a death", subjects=[npc.anchor])
    store.commit()

    removed, retired = store.forget(npc.anchor)
    assert (removed, retired) == (1, 2)
    assert [r["text"] for r in store.canon(npc.anchor)] == list(npc.canon)
    assert store.node(npc.anchor) is not None
    store.close()


def test_forget_resets_the_canonist_gate(tmp_path, theme):
    store = Store(tmp_path / "w.db")
    ensure_npcs(store, theme)
    npc = theme.npcs[0]
    store.remember(1, "a death", subjects=[npc.anchor])
    store.commit()
    canonist.refresh(store, theme, Recorder(structured_result={"facts": ["Keeps the ledger in a locked case."]}),
                     Settings().director, run_id=2)

    store.forget(npc.anchor)
    assert json.loads(store.node(npc.anchor)["data"])["canon_memories"] == 0
    store.close()


def test_forgetting_one_npc_leaves_the_other_alone(tmp_path, theme):
    store = Store(tmp_path / "w.db")
    ensure_npcs(store, theme)
    a, b = theme.npcs[0], theme.npcs[1]
    store.add_canon(a.anchor, "a-derived", "derived")
    store.add_canon(b.anchor, "b-derived", "derived")
    store.commit()

    store.forget(a.anchor)
    assert [r["text"] for r in store.canon(b.anchor, provenance="derived")] == ["b-derived"]
    store.close()
