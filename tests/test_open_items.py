"""M13: the open items, closed.

Each test names where its item was recorded -- a milestone's findings or a
playtest -- so a failure here says which promise broke. The judge tests are
about what code does with a ruling; the live measurement of what the model now
writes is in docs/tasks/M13-open-items-tasks.md.
"""

from __future__ import annotations

import pytest

from simulacra.config import Settings
from simulacra.engine.combat import GUARD_BONUS, GUARDED, player_defense
from simulacra.engine.events import FloorDescended, FloorNamed, Line, Roll
from simulacra.engine.judge import adjudicate, apply_verdict
from simulacra.engine.loop import Engine
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.world.model import Item, RoomKind
from simulacra.world.theme import Theme

from conftest import FakeClient
from test_judge import FakeState, Rigged, mob, verdict

# Both verbatim from the records.
M11_ECHO = "Tipping a lit brazier into standing water to scald something is 12."
M11_1_DODGE = "Dodging prevents the enemy from attacking, reduces damage taken by 3."


def ruling(reason, *, affects="you", kind="defend", plausible=True):
    return FakeClient(structured_result={
        "plausible": plausible, "difficulty": 10, "affects": affects, "kind": kind,
        "magnitude": 2, "reason": reason})


def judge(theme, action, reason, room="the Threshold. nothing hostile is here", **kw):
    return adjudicate(action, room, theme, ruling(reason, **kw), Settings().judge)


class Capturing(FakeClient):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.prompts: list[str] = []

    def structured(self, messages, schema, policy, *, kind="structured", retries=1):
        self.prompts.append(messages[-1]["content"])
        return super().structured(messages, schema, policy, kind=kind, retries=retries)


def offline(tmp_path, theme, client=None):
    settings = Settings()
    store = Store(tmp_path / "w.db")
    state = new_run(store, theme, settings, seed=3)
    return Engine(state, store, settings, theme, client=client), state, store


# -- the judge's reason: guarded like the narrator's prose -------------------


def test_a_reason_that_recites_the_prompt_is_refused(theme):
    """M11's pre-fix replay answered `hide` with the calibration anchors."""
    v = judge(theme, "hide", M11_ECHO)
    assert not v.plausible
    assert "brazier" not in v.reason


def test_the_players_own_words_are_not_an_echo(theme):
    """The anchors' nouns are fair when the player brought them."""
    v = judge(theme, "tip the brazier into the standing water",
              "The brazier hisses as it meets the standing water.",
              room="a flooded crypt", affects="enemy", kind="damage")
    assert v.plausible


def test_numbers_the_engine_never_agreed_to_are_dropped(theme):
    """M11.1. The engine clamps amounts after the model answers, so a number in
    the reason may not be the one applied."""
    assert judge(theme, "dodge", M11_1_DODGE).reason == \
        "Dodging prevents the enemy from attacking."


def test_a_reason_that_is_only_numbers_is_kept(theme):
    assert judge(theme, "count", "It takes 3 tries.").reason == "It takes 3 tries."


# -- the vocabulary: what the reason promises, the engine can do -------------


@pytest.mark.parametrize("affects", ["you", "enemy", "nobody"])
def test_defend_is_the_players_guard_whoever_it_names(theme, affects):
    """`dodge` has reached the judge since M11.1, and there was no defensive
    effect to give it. Probed live, the model pairs `defend` with `enemy` --
    the one a defence is against -- which mapped to nothing."""
    v = judge(theme, "raise the pick handle to block", "You get the handle up in time.",
              affects=affects)
    assert v.effect == "guard_self"


def test_the_prompt_glosses_each_kind(theme):
    client = Capturing()
    adjudicate("trip it", "r", theme, client, Settings().judge)
    assert "shields you from the next blow" in client.prompts[0]
    assert "only with something that heals" in client.prompts[0]


def test_a_guard_covers_the_round_it_was_made_against():
    state = FakeState(actors=[mob()])
    before = player_defense(state.player)
    events = apply_verdict(verdict(effect="guard_self"), state, Rigged(20))
    assert player_defense(state.player) == before + GUARD_BONUS
    assert state.player.effects[GUARDED] == 1
    assert any(isinstance(e, Line) and "guard" in e.text for e in events)


def test_the_guard_is_gone_after_the_round(tmp_path, theme):
    engine, state, store = offline(tmp_path, theme)
    state.player.effects[GUARDED] = 1
    list(engine._tick_effects())
    assert GUARDED not in state.player.effects
    store.close()


def test_an_enemy_left_open_is_told_so():
    """Applied silently before M13: the player never learned it landed."""
    state = FakeState(actors=[mob()])
    events = apply_verdict(verdict(effect="status_target", magnitude=2), state, Rigged(20))
    assert state.room.actors[0].defense == 8
    assert any(isinstance(e, Line) and "left open" in e.text for e in events)


def test_a_success_that_changes_nothing_says_so():
    events = apply_verdict(verdict(effect="nothing", magnitude=0), FakeState(), Rigged(20))
    assert isinstance(events[-1], Line)


def test_a_heal_needs_something_to_heal_with():
    """Measured live in M13: with nothing carried, "pour water over my head" was
    ruled a heal five times in five. No source, no roll -- as M11 did for a
    verdict aimed at an enemy that isn't there."""
    state = FakeState(hp=10)
    events = apply_verdict(verdict(effect="heal_self", magnitude=3), state, Rigged(20))
    assert state.player.hp == 10
    assert not any(isinstance(e, Roll) for e in events)

    state.player.inventory.append(Item(id="i:canteen", name="a canteen", heal=4))
    apply_verdict(verdict(effect="heal_self", magnitude=3), state, Rigged(20))
    assert state.player.hp == 13


# -- what the judge can see ---------------------------------------------------


def test_the_judge_is_told_what_is_carried(theme):
    """M11.1: `water` was ruled "pouring water onto the floor to cool off" with
    no water carried."""
    client = Capturing()
    adjudicate("pour water", "r", theme, client, Settings().judge)
    adjudicate("pour water", "r", theme, client, Settings().judge,
               carrying=["a canteen", "a knife"])
    assert "Carrying: nothing" in client.prompts[0]
    assert "Carrying: a canteen, a knife" in client.prompts[1]


def test_the_engine_hands_the_judge_the_pack(tmp_path, theme):
    client = Capturing(structured_result={
        "plausible": False, "difficulty": 20, "affects": "nobody", "kind": "damage",
        "magnitude": 0, "reason": "No."})
    engine, state, store = offline(tmp_path, theme, client)
    list(engine.begin())
    state.player.inventory.append(Item(id="i:canteen", name="a dented canteen"))

    list(engine.turn("jump"))
    judged = [p for p in client.prompts if "Action:" in p]
    assert judged and "a dented canteen" in judged[-1]
    # The summary is the parser fallback's context too, and anything in it is
    # a target for the fallback to lift.
    assert "a dented canteen" not in engine._room_summary()
    store.close()


# -- floor identity -----------------------------------------------------------


def test_the_first_floor_is_named_to_the_renderers(tmp_path, theme):
    """M5's recorded gap: floor 1's name and goal never reached the header."""
    engine, _, store = offline(tmp_path, theme)
    events = list(engine.begin())
    named = [e for e in events if isinstance(e, FloorNamed)]
    assert len(named) == 1 and named[0].depth == 1
    assert named[0].theme_name == theme.floor_title(1)
    assert not any(isinstance(e, FloorDescended) for e in events), \
        "a descent event would print 'You descend to floor 1' in the REPL"
    store.close()


def test_an_offline_descent_uses_the_same_title(tmp_path, theme):
    """It said "floor 2" where the header would say the theme's own title."""
    engine, state, store = offline(tmp_path, theme)
    list(engine.begin())
    state.room_id = next(r.id for r in state.floor.rooms.values()
                         if r.kind is RoomKind.DESCENT)
    descended = [e for e in engine.descend() if isinstance(e, FloorDescended)]
    assert descended[0].theme_name == theme.floor_title(2)
    store.close()


def test_hardpan_falls_back_to_a_title_of_its_own():
    """M12: two of three measured Hardpan floors were "Floor N"."""
    assert Theme.load("hardpan").floor_title(2) == "The Second Level"
