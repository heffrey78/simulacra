"""Director: tier 3, and the defensive application of its output.

A 1.7b model asked for structured floor identity will invent room ids, drop
fields and occasionally fail outright. None of that may break a run.
"""

from __future__ import annotations

import random

import pytest

from simulacra.config import Settings
from simulacra.world.director import NOTABLE, apply_floor_plan, direct_floor
from simulacra.world.floorgen import generate_floor
from simulacra.world.model import RoomKind

from conftest import FakeClient


@pytest.fixture
def floor(theme):
    return generate_floor(6, theme, random.Random(11))


def plan(floor, n=2, **over):
    ids = [r.id for r in floor.rooms.values() if r.kind in NOTABLE][:n]
    base = {
        "theme_name": "The Reprinted Ward",
        "goal": "Find the master copy.",
        "motifs": ["ink", "damp", "repetition"],
        "rooms": [
            {"id": rid, "name": f"name {i}", "concept": f"concept {i}"}
            for i, rid in enumerate(ids)
        ],
    }
    base.update(over)
    return base


def test_applies_identity_and_concepts(floor, theme):
    client = FakeClient(structured_result=plan(floor))
    assert direct_floor(floor, theme, client, Settings().director) is True

    assert floor.theme_name == "The Reprinted Ward"
    assert floor.goal == "Find the master copy."
    assert floor.motifs == ("ink", "damp", "repetition")
    assert sum(1 for r in floor.rooms.values() if r.concept) == 2


def test_invented_room_ids_are_dropped(floor, theme):
    p = plan(floor)
    p["rooms"].append({"id": "d99r99", "name": "nowhere", "concept": "invented"})
    client = FakeClient(structured_result=p)

    direct_floor(floor, theme, client, Settings().director)
    assert not any(r.concept == "invented" for r in floor.rooms.values())


def test_corridors_are_excluded_from_the_census(floor, theme):
    """Corridors are over half a deep floor and need no identity. Including them
    would multiply the most expensive call in the game for nothing."""
    from simulacra.world.director import _census
    # The census takes the theme since M12: it describes each room in the
    # theme's words rather than the engine's role names.
    census, ids = _census(floor, theme)
    corridors = [r.id for r in floor.rooms.values() if r.kind is RoomKind.CORRIDOR]
    assert corridors
    for rid in corridors:
        assert rid not in census
        assert rid not in ids


def test_failure_is_non_fatal_and_leaves_the_floor_playable(floor, theme):
    original = {r.id: r.name for r in floor.rooms.values()}
    client = FakeClient(structured_result=RuntimeError("model down"))

    assert direct_floor(floor, theme, client, Settings().director) is False
    assert floor.theme_name == ""
    assert {r.id: r.name for r in floor.rooms.values()} == original


@pytest.mark.parametrize("generic", ["Entrance", "Chamber", "Descent", "the shrine",
                                     "Room", "corridor", "Stairs."])
def test_generic_room_names_are_rejected(floor, generic):
    """Regression from the first live M2 run: asked for a room name, the model
    restated the structural role, replacing the theme pack's far better pools."""
    room = next(r for r in floor.rooms.values() if r.kind in NOTABLE)
    original = room.name
    apply_floor_plan(floor, {
        "theme_name": "X", "goal": "", "motifs": [],
        "rooms": [{"id": room.id, "name": generic, "concept": "a real concept"}],
    })
    assert room.name == original, "a generic name overwrote the theme's own"
    assert room.concept == "a real concept", "the concept should still apply"


def test_evocative_room_names_are_accepted(floor):
    room = next(r for r in floor.rooms.values() if r.kind in NOTABLE)
    apply_floor_plan(floor, {
        "theme_name": "X", "goal": "", "motifs": [],
        "rooms": [{"id": room.id, "name": "The Unworn Place", "concept": "c"}],
    })
    assert room.name == "The Unworn Place"


@pytest.mark.parametrize("junk", [None, [], "a string", 42])
def test_malformed_responses_are_survived(floor, junk):
    assert apply_floor_plan(floor, junk) is False


def test_partial_response_still_contributes(floor):
    # No rooms at all, but a usable floor name.
    assert apply_floor_plan(floor, {"theme_name": "The Annex", "goal": "", "motifs": [], "rooms": []})
    assert floor.theme_name == "The Annex"


def test_motifs_are_capped_at_three(floor):
    apply_floor_plan(floor, plan(floor, motifs=["a", "b", "c", "d", "e"]))
    assert len(floor.motifs) == 3


def test_previous_floors_are_sent_as_a_do_not_repeat_list(floor, theme):
    """Small models converge hard on one idea without this."""
    captured = {}

    class Capturing(FakeClient):
        def structured(self, messages, schema, policy, *, kind="structured", retries=1):
            captured["user"] = messages[-1]["content"]
            return plan(floor)

    direct_floor(floor, theme, Capturing(), Settings().director,
                 previously=["The Drowned Stacks", "The Annex"])
    assert "The Drowned Stacks" in captured["user"]
    assert "The Annex" in captured["user"]
