"""Narrator, and specifically the echo guard.

M0 benchmarking caught `qwen3:1.7b` returning the room census back verbatim
instead of narrating -- intermittently, from a prompt that had worked moments
before. These tests pin the detection and both recovery paths.
"""

from __future__ import annotations

import random

import pytest

from simulacra.config import Settings
from simulacra.memory.store import Store
from simulacra.narrate.narrator import GUARD_PREFIX_CHARS, Narrator, looks_like_echo
from simulacra.world.floorgen import generate_floor

from conftest import FakeClient

GOOD = "Water has got in and never left. Your boots find the edge of something submerged."


@pytest.fixture
def floor(theme):
    return generate_floor(1, theme, random.Random(42))


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "n.db")
    yield s
    s.close()


def make(client, theme, store):
    return Narrator(client, theme, store, Settings().narrator)


# -- detection -------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "ROOM: the Threshold\nROLE: entrance\nEXITS: north",
    "Room: the Threshold. Exits: north. A jar of clean water lies here.",
    "EXITS: north, east",
    "",
    "   ",
])
def test_census_echoes_are_detected(text):
    census = "ROOM: the Threshold\nROLE: entrance\nCONTAINS: a jar\nEXITS: north"
    assert looks_like_echo(text, census)


@pytest.mark.parametrize("text", [
    GOOD,
    "The threshold stone is worn into a dip. Someone stood here a very long time.",
    "A jar of clean water sits untouched, which is the strangest thing in the room.",
])
def test_real_prose_is_not_flagged(text):
    census = "ROOM: the Threshold\nROLE: entrance\nCONTAINS: a jar\nEXITS: north"
    assert not looks_like_echo(text, census)


def test_verbatim_prefix_of_the_census_is_an_echo():
    census = "ROOM: the drowned gallery and its long shelves of silt"
    assert looks_like_echo("the drowned gallery and its long shelves", census)


# -- recovery --------------------------------------------------------------


def test_short_echo_triggers_a_strict_retry(floor, theme, store):
    """Echo shorter than the guard prefix: caught at end-of-stream, retried."""
    client = FakeClient(script=["EXITS: north, east", GOOD])
    narrator = make(client, theme, store)

    out = "".join(narrator.room(floor, floor.room(floor.entrance_id)))
    assert out == GOOD
    assert client.stream_calls == 2  # first rejected, second accepted


def test_long_echo_is_cut_off_before_reaching_the_player(floor, theme, store):
    """Echo longer than the guard prefix: caught mid-stream and abandoned."""
    long_echo = "ROOM: First Landing ROLE: entrance EXITS: north, east, south " * 3
    client = FakeClient(script=[long_echo, GOOD])
    narrator = make(client, theme, store)

    out = "".join(narrator.room(floor, floor.room(floor.entrance_id)))
    assert len(long_echo) > GUARD_PREFIX_CHARS
    assert "EXITS" not in out
    assert out == GOOD


def test_two_failures_fall_back_without_showing_the_player_an_error(floor, theme, store):
    client = FakeClient(script=["EXITS: north"])  # always an echo
    narrator = make(client, theme, store)
    room = floor.room(floor.entrance_id)

    out = "".join(narrator.room(floor, room))
    assert out  # something readable
    assert "EXITS" not in out


def test_good_prose_reaches_the_player_intact(floor, theme, store):
    client = FakeClient(script=[GOOD])
    narrator = make(client, theme, store)
    assert "".join(narrator.room(floor, floor.room(floor.entrance_id))) == GOOD


# -- caching ---------------------------------------------------------------


def test_second_visit_costs_no_model_call(floor, theme, store):
    client = FakeClient(script=[GOOD])
    narrator = make(client, theme, store)
    room = floor.room(floor.entrance_id)

    first = "".join(narrator.room(floor, room))
    calls = client.stream_calls
    second = "".join(narrator.room(floor, room))

    assert first == second
    assert client.stream_calls == calls, "revisiting a room re-generated its prose"


def test_rejected_prose_is_never_cached(floor, theme, store):
    client = FakeClient(script=["EXITS: north"])
    narrator = make(client, theme, store)
    room = floor.room(floor.entrance_id)

    list(narrator.room(floor, room))
    assert store.cached_prose(narrator._key(floor, room)) is None


def test_changing_the_concept_invalidates_cached_prose(floor, theme, store):
    client = FakeClient(script=[GOOD])
    narrator = make(client, theme, store)
    room = floor.room(floor.entrance_id)

    before = narrator._key(floor, room)
    room.concept = "a room that has been copied one time too many"
    assert narrator._key(floor, room) != before


# -- prompt ----------------------------------------------------------------


def test_census_is_labelled_data_not_prose(floor, theme, store):
    """Labels are what make an echo detectable and stop the model continuing it."""
    narrator = make(FakeClient(), theme, store)
    room = next(r for r in floor.rooms.values() if r.items)
    census = narrator._census(floor, room)

    assert census.startswith("ROOM:")
    assert "EXITS:" in census
    assert "CONTAINS:" in census
    assert room.items[0].name in census


def test_banned_words_reach_the_system_prompt(floor, theme, store):
    narrator = make(FakeClient(), theme, store)
    system = narrator._system()
    assert theme.banned[0] in system


# -- degeneracy ------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "1234567890: 1234567890 count: 1234567890 you: 1234567890 wear: 1234567890",
    "wear: x\nseams: x\nresidue: x\nwrongness: x\ncopies: x",
    "the the the the the the the the the the",
])
def test_degenerate_output_is_detected(text):
    """The Archivist's first live reply was the motif list with '1234567890'
    against every entry. The echo guard did not catch it -- this does."""
    from simulacra.narrate.narrator import looks_degenerate
    assert looks_degenerate(text)


@pytest.mark.parametrize("text", [
    GOOD,
    "Two hundred and nine went down. Two hundred and nine did not come back.",
    "You are the fourth this month. The others are still down there, in a sense.",
])
def test_real_writing_is_not_flagged_as_degenerate(text):
    from simulacra.narrate.narrator import looks_degenerate
    assert not looks_degenerate(text)


def test_dialogue_prompts_omit_the_motif_list(theme):
    """A comma-separated motif list reads to a small model as a form to fill in."""
    assert "Motifs:" in theme.style_note()
    assert "Motifs:" not in theme.style_note(motifs=False)
    assert theme.banned[0] in theme.style_note(motifs=False)


def test_degenerate_room_prose_is_rejected(floor, theme, store):
    client = FakeClient(script=["a a a a a a a a a a a a", GOOD])
    narrator = make(client, theme, store)
    out = "".join(narrator.room(floor, floor.room(floor.entrance_id)))
    assert out == GOOD
