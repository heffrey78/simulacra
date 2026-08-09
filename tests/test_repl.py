"""Renderer. Thin, but it is the one place that can silently eat output."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from simulacra.engine.events import (
    Damage,
    Line,
    Notice,
    ProseDelta,
    ProseEnd,
    ProseStart,
    Roll,
    RoomEntered,
    RunEnded,
    StatusChanged,
)
from simulacra.ui.repl import ReplRenderer


def render(*events) -> str:
    buf = io.StringIO()
    r = ReplRenderer(Console(file=buf, width=100, highlight=False,
                             markup=False, no_color=True))
    for e in events:
        r.handle(e)
    return buf.getvalue()


def test_roll_lines_survive_their_square_brackets():
    """Regression: rich read '[attack: 15 vs 10 -> hit]' as a style tag and
    printed nothing. Found only when M3 first emitted a Roll for real."""
    out = render(Roll(label="attack", total=15, target=10, success=True,
                      detail="bare-handed"))
    assert "attack" in out and "15" in out and "10" in out
    assert "hit" in out


def test_prose_containing_brackets_is_not_swallowed():
    """Model output is untrusted -- it must not be able to inject markup."""
    out = render(ProseStart(), ProseDelta(text="a sign reading [DO NOT ENTER]"), ProseEnd())
    assert "DO NOT ENTER" in out


def test_damage_reports_amount_and_remaining():
    out = render(Damage(target="a ghoul", amount=4, hp_left=2))
    assert "a ghoul" in out and "4" in out and "2" in out


def test_exits_render_in_a_stable_order_not_generation_order():
    out = render(RoomEntered(room_id="r", name="a room",
                             exits=("south", "north", "west"), first_visit=True))
    line = next(x for x in out.splitlines() if "exits" in x)
    # Clockwise N/E/S/W, per the M1 spec -- not the room dict's generation order.
    assert line.index("N") < line.index("S") < line.index("W")


def test_run_ended_shows_cause_and_epitaph():
    out = render(RunEnded(cause="a ghoul", depth=4, turns=88, epitaph="Numbered, then not."))
    assert "a ghoul" in out and "4" in out and "88" in out
    assert "Numbered, then not." in out


def test_prose_deltas_do_not_each_get_a_newline():
    out = render(ProseStart(), ProseDelta(text="one "), ProseDelta(text="two"), ProseEnd())
    assert "one two" in out


def test_unknown_events_are_ignored_silently():
    class Invented:
        pass

    assert render(Invented()) == ""


def test_status_line_includes_effects():
    out = render(StatusChanged(hp=8, max_hp=20, depth=3, effects=("braced",)))
    assert "8" in out and "20" in out and "braced" in out


@pytest.mark.parametrize("style", ["normal", "dim", "alert", "good", "title"])
def test_every_line_style_renders(style):
    assert "text" in render(Line(text="text", style=style))


def test_notice_renders():
    assert "no way" in render(Notice(text="no way", level="warn"))


def test_damage_to_the_player_reads_as_second_person():
    assert "you take 3" in render(Damage(target="you", amount=3, hp_left=5))
    assert "a ghoul takes 3" in render(Damage(target="a ghoul", amount=3, hp_left=5))
