"""The judge. Every test here is about bounding what the model can do to state."""

from __future__ import annotations

import random

import pytest

from simulacra.config import Settings
from simulacra.engine.events import Damage, Improvised, Roll
from simulacra.engine.judge import (
    MAX_MAGNITUDE,
    Verdict,
    _ANCHORS,
    adjudicate,
    apply_verdict,
)
from simulacra.world.model import Actor, Player

from conftest import FakeClient


class FakeState:
    def __init__(self, actors=None, hp=20):
        self.player = Player(hp=hp)
        self.depth = 3
        self._last_action = "tip the brazier into the water"

        class Room:
            pass

        self.room = Room()
        self.room.actors = actors if actors is not None else []


def mob(hp=10, name="a duplicate"):
    return Actor(id="m", name=name, hp=hp, max_hp=hp, attack=2, defense=10)


def verdict(**kw):
    base = dict(plausible=True, difficulty=10, effect="damage_target",
                magnitude=3, reason="steam scalds it")
    base.update(kw)
    return Verdict(**base)


ALWAYS_HIT = random.Random(0)


class Rigged(random.Random):
    def __init__(self, roll):
        super().__init__(0)
        self._roll = roll

    def randint(self, a, b):
        return self._roll


# -- clamping --------------------------------------------------------------


@pytest.mark.parametrize("effect,cap", sorted(MAX_MAGNITUDE.items()))
def test_magnitude_is_clamped_however_large_the_model_goes(theme, effect, cap):
    client = FakeClient(structured_result={
        "plausible": True, "difficulty": 10, "effect": effect,
        "magnitude": 999, "reason": "r",
    })
    v = adjudicate("x", "a room", theme, client, Settings().judge)
    assert v.magnitude == cap


def test_difficulty_is_clamped_into_range(theme):
    client = FakeClient(structured_result={
        "plausible": True, "difficulty": 900, "effect": "nothing",
        "magnitude": 0, "reason": "r"})
    assert adjudicate("x", "r", theme, client, Settings().judge).difficulty == 20


def test_unknown_effect_becomes_nothing(theme):
    client = FakeClient(structured_result={
        "plausible": True, "difficulty": 10, "effect": "delete_the_dungeon",
        "magnitude": 8, "reason": "r"})
    v = adjudicate("x", "r", theme, client, Settings().judge)
    assert v.effect == "nothing"


@pytest.mark.parametrize("affects,kind,expected", [
    ("enemy", "damage", "damage_target"),
    ("enemy", "hinder", "status_target"),
    ("you", "damage", "damage_self"),
    ("you", "heal", "heal_self"),
    ("you", "hinder", "status_self"),
    ("nobody", "damage", "nothing"),
    ("enemy", "heal", "nothing"),      # healing the enemy quietly does not exist
    ("sideways", "damage", "nothing"),
])
def test_two_field_verdicts_map_onto_the_closed_vocabulary(theme, affects, kind, expected):
    """The model answers with two simple choices; the engine keeps its own enum.
    Asked for one six-way compound name it contradicted its own reason in 3 of 5
    live samples."""
    client = FakeClient(structured_result={
        "plausible": True, "difficulty": 10, "affects": affects,
        "kind": kind, "magnitude": 2, "reason": "r"})
    assert adjudicate("x", "r", theme, client, Settings().judge).effect == expected


def test_the_prompt_asks_for_the_two_field_form(theme):
    captured = {}

    class Capturing(FakeClient):
        def structured(self, messages, schema, policy, *, kind="structured", retries=1):
            captured["schema"] = schema
            captured["user"] = messages[-1]["content"]
            return {"plausible": False, "difficulty": 20, "affects": "nobody",
                    "kind": "damage", "magnitude": 0, "reason": "r"}

    adjudicate("x", "r", theme, Capturing(), Settings().judge)
    assert set(captured["schema"]["required"]) >= {"affects", "kind"}
    assert "affects" in captured["user"]


# -- failure modes ---------------------------------------------------------


def test_an_exception_is_never_a_free_win(theme):
    client = FakeClient(structured_result=RuntimeError("model down"))
    v = adjudicate("x", "r", theme, client, Settings().judge)
    assert v.plausible is False and v.magnitude == 0


@pytest.mark.parametrize("junk", [None, "a string", 42, {}, {"effect": "damage_target"}])
def test_malformed_responses_never_become_a_free_win(theme, junk):
    client = FakeClient(structured_result=junk)
    v = adjudicate("x", "r", theme, client, Settings().judge)
    assert not (v.plausible and v.magnitude > 0)


def test_reason_is_never_empty(theme):
    client = FakeClient(structured_result={
        "plausible": True, "difficulty": 10, "effect": "nothing",
        "magnitude": 0, "reason": "   "})
    assert adjudicate("x", "r", theme, client, Settings().judge).reason


# -- calibration -----------------------------------------------------------


def test_difficulty_anchors_are_in_the_prompt(theme):
    """The M0 benchmark rated a clever action at 20 -- the schema maximum --
    twice. Anchors are the fix; without them there is nothing to interpolate."""
    captured = {}

    class Capturing(FakeClient):
        def structured(self, messages, schema, policy, *, kind="structured", retries=1):
            captured["system"] = messages[0]["content"]
            return {"plausible": True, "difficulty": 12, "effect": "nothing",
                    "magnitude": 0, "reason": "r"}

    adjudicate("tip the brazier", "a flooded crypt", theme, Capturing(), Settings().judge)
    assert _ANCHORS.strip() in captured["system"]
    assert "8" in captured["system"] and "18" in captured["system"]


def test_theme_voice_still_reaches_the_judge(theme):
    captured = {}

    class Capturing(FakeClient):
        def structured(self, messages, schema, policy, *, kind="structured", retries=1):
            captured["system"] = messages[0]["content"]
            return {"plausible": False, "difficulty": 20, "effect": "nothing",
                    "magnitude": 0, "reason": "r"}

    adjudicate("x", "r", theme, Capturing(), Settings().judge)
    assert theme.judge_system.split(".")[0] in captured["system"]


# -- application -----------------------------------------------------------


def test_implausible_actions_do_nothing_at_all():
    state = FakeState(actors=[mob()])
    events = apply_verdict(verdict(plausible=False), state, Rigged(20))
    assert len(events) == 1 and isinstance(events[0], Improvised)
    assert state.room.actors[0].hp == 10


def test_a_failed_roll_applies_no_effect():
    state = FakeState(actors=[mob()])
    apply_verdict(verdict(difficulty=20), state, Rigged(1))  # natural 1
    assert state.room.actors[0].hp == 10


def test_a_successful_roll_damages_the_target():
    state = FakeState(actors=[mob(hp=10)])
    events = apply_verdict(verdict(magnitude=3), state, Rigged(20))
    assert state.room.actors[0].hp == 7
    assert any(isinstance(e, Damage) for e in events)
    assert any(isinstance(e, Roll) for e in events)


def test_damage_target_with_no_target_is_harmless():
    state = FakeState(actors=[])
    apply_verdict(verdict(effect="damage_target"), state, Rigged(20))
    assert state.player.hp == 20


def test_heal_cannot_exceed_max_hp():
    state = FakeState(hp=19)
    apply_verdict(verdict(effect="heal_self", magnitude=3), state, Rigged(20))
    assert state.player.hp == state.player.max_hp


def test_damage_self_can_kill_you():
    state = FakeState(hp=2)
    apply_verdict(verdict(effect="damage_self", magnitude=4), state, Rigged(20))
    assert state.player.hp <= 0
