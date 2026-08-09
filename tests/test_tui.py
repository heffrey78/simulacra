"""The Textual frontend.

Skipped wholesale when the `tui` extra is absent -- the UI is optional and a
missing optional dependency is not a test failure. `pytest` must stay green on a
headless, offline box with Ollama stopped, which is why every engine here is
fake: these tests are about the *renderer*, exactly like test_repl.py.

Assertions are on what a panel was told, never on pixel layout.
"""

from __future__ import annotations

import asyncio
import functools
import threading

import pytest

pytest.importorskip("textual", reason="needs the 'tui' extra: pip install -e '.[tui]'")

from simulacra.engine.events import (  # noqa: E402
    Damage,
    FloorDescended,
    Line,
    Notice,
    ProseDelta,
    ProseEnd,
    ProseStart,
    RoomEntered,
    RunEnded,
    StatusChanged,
    Thinking,
)
from simulacra.ui.tui import (  # noqa: E402
    _FLUSH_INTERVAL,
    CommandInput,
    FogMap,
    MapPanel,
    SimulacraApp,
    StatusPanel,
)
from textual.widgets import RichLog, Static  # noqa: E402


def tui_test(fn):
    """`run_test()` is an async context manager and this project has no
    pytest-asyncio. One `asyncio.run` per test is cheaper than a plugin."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        asyncio.run(fn(*args, **kwargs))

    return wrapper


class SilentEngine:
    """Yields nothing. Tests that drive the renderer directly do not want an
    opening beat competing with them."""

    def begin(self):
        return iter(())

    def turn(self, text):
        return iter(())


class ScriptedEngine:
    """Replays a fixed list of events per turn, and records what it was asked."""

    def __init__(self, opening=(), per_turn=()):
        self.opening = list(opening)
        self.per_turn = list(per_turn)
        self.asked: list[str] = []

    def begin(self):
        yield from self.opening

    def turn(self, text):
        self.asked.append(text)
        yield from self.per_turn


def log_lines(app) -> list[str]:
    """What the prose log actually shows, one visible (wrapped) line each."""
    return [strip.text.rstrip() for strip in app.query_one("#log", RichLog).lines]


def live_text(app) -> str:
    return str(app.query_one("#live", Static).content)


# -- streamed prose --------------------------------------------------------


@tui_test
async def test_prose_deltas_accumulate_into_one_paragraph():
    """The M2 thesis, cashed. A delta per line would look like the model is
    emitting one word at a time; it is a single sentence arriving gradually."""
    app = SimulacraApp(SilentEngine())
    async with app.run_test() as pilot:
        app.dispatch(ProseStart())
        for piece in ("wet stone, ", "and a draught ", "from below."):
            app.dispatch(ProseDelta(piece))

        # Three deltas, one pending repaint. Tokens arrive at ~15/s and the
        # cores that produced them are the cores that would do the repainting.
        assert app._prose == "wet stone, and a draught from below."
        assert app._flush is not None

        # Mid-stream it shows in the growing block, as one run of text.
        await pilot.pause(_FLUSH_INTERVAL * 3)
        assert live_text(app) == "wet stone, and a draught from below."

        app.dispatch(ProseEnd())
        await pilot.pause()
        assert live_text(app) == ""
        assert "wet stone, and a draught from below." in "\n".join(log_lines(app))
        # The failure this guards: three deltas becoming three lines.
        assert "wet stone," not in log_lines(app)


@tui_test
async def test_prose_containing_brackets_is_not_swallowed():
    """Model output is untrusted input. Rich reads square brackets as style tags
    and ate the combat line once already, in M3."""
    app = SimulacraApp(SilentEngine())
    async with app.run_test() as pilot:
        app.dispatch(ProseStart())
        app.dispatch(ProseDelta("a sign reading [DO NOT ENTER]"))
        app.dispatch(ProseEnd())
        await pilot.pause()
        assert "DO NOT ENTER" in "\n".join(log_lines(app))


@tui_test
async def test_speaker_prefixes_the_paragraph():
    app = SimulacraApp(SilentEngine())
    async with app.run_test() as pilot:
        app.dispatch(ProseStart(channel="npc", speaker="the Archivist"))
        app.dispatch(ProseDelta("you came back."))
        await pilot.pause(_FLUSH_INTERVAL * 3)
        assert live_text(app) == "the Archivist: you came back."


# -- the worker ------------------------------------------------------------


@tui_test
async def test_input_is_disabled_while_a_turn_runs_and_re_enabled_after():
    """The engine is single-threaded and the client serializes requests. A
    queued turn would pile up behind a 20 s director call."""
    gate = threading.Event()
    started = threading.Event()

    class BlockingEngine(SilentEngine):
        def turn(self, text):
            started.set()
            gate.wait(5)
            yield Notice("done")

    app = SimulacraApp(BlockingEngine())
    async with app.run_test() as pilot:
        cmd = app.query_one("#cmd", CommandInput)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert cmd.disabled is False

        cmd.value = "wait"
        await pilot.press("enter")
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
        await pilot.pause()
        assert cmd.disabled is True

        gate.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert cmd.disabled is False


@tui_test
async def test_the_turn_runs_off_the_event_loop():
    """If the engine ran on the UI thread the whole point of the worker is gone,
    and the spinner covering an 18 s descent would never paint."""
    seen: list[str] = []

    class NosyEngine(SilentEngine):
        def turn(self, text):
            seen.append(threading.current_thread().name)
            return iter(())

    app = SimulacraApp(NosyEngine())
    async with app.run_test() as pilot:
        app.query_one("#cmd", CommandInput).value = "look"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert seen and seen[0] != threading.main_thread().name


# -- the thinking beat -----------------------------------------------------


@tui_test
async def test_thinking_shows_a_spinner_and_the_next_event_clears_it():
    """Cleared by the engine continuing, not by a timer. The engine is the only
    thing that knows when the blocking call returned."""
    app = SimulacraApp(SilentEngine())
    async with app.run_test() as pilot:
        app.dispatch(Thinking("The floor takes shape"))
        await pilot.pause()
        assert app.thinking is True
        assert "The floor takes shape" in str(app.query_one("#thinking", Static).content)

        app.dispatch(Notice("anything at all"))
        await pilot.pause()
        assert app.thinking is False
        assert str(app.query_one("#thinking", Static).content) == ""


@tui_test
async def test_the_spinner_timer_does_not_outlive_the_wait():
    """No idle repaint timers -- the cores belong to inference (T7)."""
    app = SimulacraApp(SilentEngine())
    async with app.run_test() as pilot:
        assert app._spinner is None
        app.dispatch(Thinking("The floor takes shape"))
        await pilot.pause()
        assert app._spinner is not None
        app.dispatch(Line("done"))
        await pilot.pause()
        assert app._spinner is None


# -- the status panel ------------------------------------------------------


@tui_test
async def test_status_panel_reflects_only_the_last_status_changed():
    app = SimulacraApp(SilentEngine())
    async with app.run_test() as pilot:
        app.dispatch(StatusChanged(hp=20, max_hp=20, depth=1))
        app.dispatch(StatusChanged(hp=6, max_hp=23, depth=4, effects=("braced",)))
        await pilot.pause()

        panel = app.query_one("#status", StatusPanel)
        text = str(panel.content)
        assert "6/23" in text and "floor 4" in text and "braced" in text
        assert "20/20" not in text and "floor 1" not in text


@tui_test
async def test_taking_damage_moves_the_hp_bar():
    """Found by playing it: combat emits no StatusChanged, so the bar sat at
    23/23 through an entire fight and was still there when the log said the
    killing blow landed. `Damage.hp_left` is the player's hp and is already in
    the stream -- no engine change, and no reading game state."""
    app = SimulacraApp(SilentEngine())
    async with app.run_test() as pilot:
        app.dispatch(StatusChanged(hp=23, max_hp=23, depth=2))
        app.dispatch(Damage(target="a smudged figure", amount=1, hp_left=8))
        await pilot.pause()
        assert "23/23" in str(app.query_one("#status", StatusPanel).content)

        app.dispatch(Damage(target="you", amount=3, hp_left=20))
        await pilot.pause()
        text = str(app.query_one("#status", StatusPanel).content)
        assert "20/23" in text


@tui_test
async def test_floor_name_and_goal_stay_on_screen():
    """In the REPL the tier-3 goal scrolls away after one line, which wastes the
    call that produced it."""
    app = SimulacraApp(SilentEngine())
    async with app.run_test() as pilot:
        app.dispatch(FloorDescended(depth=2, theme_name="The Salt Galleries",
                                    goal="Find what the tide left."))
        app.dispatch(StatusChanged(hp=9, max_hp=23, depth=2))
        await pilot.pause()
        text = str(app.query_one("#status", StatusPanel).content)
        assert "The Salt Galleries" in text
        assert "Find what the tide left." in text


@tui_test
async def test_the_hp_bar_changes_colour_when_it_gets_bad():
    app = SimulacraApp(SilentEngine())
    async with app.run_test() as pilot:
        panel = app.query_one("#status", StatusPanel)

        app.dispatch(StatusChanged(hp=20, max_hp=20, depth=1))
        await pilot.pause()
        healthy = {str(s.style) for s in panel.content.spans}

        app.dispatch(StatusChanged(hp=2, max_hp=20, depth=1))
        await pilot.pause()
        hurt = {str(s.style) for s in panel.content.spans}

        assert "green" in healthy
        assert "green" not in hurt and any("red" in s for s in hurt)


# -- the map ---------------------------------------------------------------


def test_fog_map_places_rooms_by_dead_reckoning():
    """Three moves, three rooms, in the right relative positions."""
    m = FogMap()
    m.enter("a", ("north", "east"), None)
    m.enter("b", ("south", "east"), "north")
    m.enter("c", ("west", "south"), "east")
    m.enter("d", ("north",), "south")

    assert m.pos == {"a": (0, 0), "b": (0, -1), "c": (1, -1), "d": (1, 0)}
    assert m.current == "d"


def test_fog_map_shows_only_what_was_walked():
    """Fog of war for free: an exit you have not taken is a stub, not a room."""
    m = FogMap()
    m.enter("a", ("north", "east"), None)
    assert set(m.pos) == {"a"}
    assert m._stubs() == {(0, -1), (1, 0)}


def test_revisiting_a_room_keeps_its_square():
    m = FogMap()
    m.enter("a", ("north",), None)
    m.enter("b", ("south",), "north")
    m.enter("a", ("north",), "south")
    assert m.pos == {"a": (0, 0), "b": (0, -1)}
    assert m.current == "a"


def test_a_look_does_not_move_anything():
    """`look` re-emits RoomEntered with via=None for a room already drawn."""
    m = FogMap()
    m.enter("a", ("north",), None)
    m.enter("b", ("south",), "north")
    m.enter("b", ("south",), None)          # look
    assert m.pos == {"a": (0, 0), "b": (0, -1)}


def test_two_rooms_reckoning_onto_one_square_leaves_one_room():
    """Floors are not planar -- floorgen picks each connection's direction at
    random -- so this is a real dungeon, not a corrupt map. One square holds one
    room and the newer sighting wins."""
    m = FogMap()
    m.enter("a", ("north", "east"), None)
    m.enter("b", ("south", "east"), "north")
    m.enter("c", ("west",), "east")          # (1, -1)
    m.enter("d", ("north",), "west")         # back onto (0, -1), where b sits
    assert m.pos["d"] == (0, -1)
    assert "b" not in m.pos
    assert m.cell[(0, -1)] == "d"


def test_map_renders_links_and_stubs():
    m = FogMap()
    m.enter("a", ("north", "east"), None)
    m.enter("b", ("south", "west"), "north")
    lines = m.render_lines()
    assert lines == ["·─▣  ", "  │  ", "  ▢─·"]


@tui_test
async def test_map_panel_is_built_from_events_and_resets_on_descent():
    app = SimulacraApp(SilentEngine())
    async with app.run_test() as pilot:
        panel = app.query_one("#map", MapPanel)
        app.dispatch(RoomEntered(room_id="a", name="the mouth", exits=("north",),
                                 first_visit=True))
        app.dispatch(RoomEntered(room_id="b", name="a stair", exits=("south",),
                                 first_visit=True, via="north"))
        await pilot.pause()
        assert set(panel.map.pos) == {"a", "b"}

        app.dispatch(FloorDescended(depth=2, theme_name="Below", goal=""))
        await pilot.pause()
        assert panel.map.pos == {}
        assert "unmapped" in str(panel.content)

        app.dispatch(RoomEntered(room_id="c", name="a new mouth", exits=("east",),
                                 first_visit=True))
        await pilot.pause()
        assert panel.map.pos == {"c": (0, 0)}


# -- input and keybindings -------------------------------------------------


@tui_test
async def test_a_movement_key_submits_the_string_a_player_could_have_typed():
    """No bypassing the parser. If a keybinding reached the engine by another
    route the TUI and the REPL would diverge, and stage-2 inference would stop
    being exercised."""
    engine = ScriptedEngine()
    app = SimulacraApp(engine)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("n")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert engine.asked == ["north"]


@tui_test
async def test_a_movement_key_is_a_letter_when_you_are_mid_word():
    """Otherwise you could never type 'north'."""
    engine = ScriptedEngine()
    app = SimulacraApp(engine)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        cmd = app.query_one("#cmd", CommandInput)
        cmd.value = "ope"
        await pilot.press("n")
        await pilot.pause()
        assert cmd.value == "open"
        assert engine.asked == []


@tui_test
async def test_command_history_walks_backwards_and_forwards():
    engine = ScriptedEngine()
    app = SimulacraApp(engine)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        cmd = app.query_one("#cmd", CommandInput)
        for text in ("look", "take lamp"):
            cmd.value = text
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

        await pilot.press("up")
        assert cmd.value == "take lamp"
        await pilot.press("up")
        assert cmd.value == "look"
        await pilot.press("down")
        assert cmd.value == "take lamp"
        await pilot.press("down")
        assert cmd.value == ""


@tui_test
async def test_submitting_echoes_the_input_into_the_log():
    engine = ScriptedEngine()
    app = SimulacraApp(engine)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        app.query_one("#cmd", CommandInput).value = "talk to the archivist"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert any("> talk to the archivist" in line for line in log_lines(app))


@tui_test
async def test_an_empty_line_is_not_a_turn():
    engine = ScriptedEngine()
    app = SimulacraApp(engine)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("enter")
        await pilot.pause()
    assert engine.asked == []


@tui_test
async def test_ctrl_l_clears_the_log():
    app = SimulacraApp(SilentEngine())
    async with app.run_test() as pilot:
        app.dispatch(Line("something"))
        await pilot.pause()
        assert log_lines(app)
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert log_lines(app) == []


# -- the end of a run ------------------------------------------------------


@tui_test
async def test_a_death_holds_the_screen_and_reports_upwards():
    seen = []
    app = SimulacraApp(SilentEngine(), on_end=seen.append)
    async with app.run_test() as pilot:
        app.dispatch(RunEnded(cause="a ghoul", depth=4, turns=88,
                              epitaph="Numbered, then not."))
        await pilot.pause()

        assert app.ended is True
        assert seen and seen[0].cause == "a ghoul"
        assert app.query_one("#cmd", CommandInput).disabled is True
        joined = "\n".join(log_lines(app))
        assert "a ghoul" in joined and "Numbered, then not." in joined


@tui_test
async def test_quitting_leaves_immediately():
    exits = []
    app = SimulacraApp(SilentEngine())
    async with app.run_test() as pilot:
        app.exit = lambda *a, **k: exits.append(True)
        app.dispatch(RunEnded(cause="quit", depth=2, turns=9))
        await pilot.pause()
    assert exits == [True]


@tui_test
async def test_input_stays_disabled_after_the_run_ends():
    """A finished run must not re-enable the prompt when the worker unwinds."""
    class DyingEngine(SilentEngine):
        def turn(self, text):
            yield RunEnded(cause="a ghoul", depth=1, turns=2)

    app = SimulacraApp(DyingEngine())
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        app.query_one("#cmd", CommandInput).value = "north"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one("#cmd", CommandInput).disabled is True


# -- the seam --------------------------------------------------------------


@tui_test
async def test_unknown_events_are_ignored_silently():
    """Same rule as repl.py. This is what lets a later milestone add an event
    without breaking the UI mid-flight."""
    class Invented:
        pass

    app = SimulacraApp(SilentEngine())
    async with app.run_test() as pilot:
        app.dispatch(Invented())
        await pilot.pause()
        assert log_lines(app) == []


@tui_test
async def test_memory_observes_the_same_stream_the_renderer_does():
    """Memory is an observer of both frontends, not a dependency of either."""
    class Recorder:
        def __init__(self):
            self.events = []

        def handle(self, event):
            self.events.append(event)

    seen = Recorder()
    engine = ScriptedEngine(opening=[Line("The Simulacra", style="title"),
                                     StatusChanged(hp=20, max_hp=20, depth=1)])
    app = SimulacraApp(engine, observers=[seen])
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert [type(e) for e in seen.events] == [Line, StatusChanged]


@tui_test
async def test_the_app_is_never_handed_game_state():
    """`GameState` is mutated on the worker thread. The panels cannot race it if
    the app has no way to reach it -- so this is a design assertion, not a
    style one."""
    app = SimulacraApp(SilentEngine())
    async with app.run_test():
        assert not hasattr(app, "state")


def test_exits_render_in_the_same_order_as_the_repl():
    """Two frontends disagreeing about exit order is a small permanent papercut."""
    from simulacra.ui import repl, tui

    assert tui._EXIT_ORDER == repl._EXIT_ORDER
    assert tui._EXIT_SHORT == repl._EXIT_SHORT
