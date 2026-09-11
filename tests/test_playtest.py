"""M11: the 2026-09-10 playtest, answered.

Each section is one question from docs/playtests/2026-09-10.md. The tests assert
on mechanism -- what the prompt contained, what was written, what was rolled --
because every one of these bugs shipped with tests green, and was found by
playing.
"""

from __future__ import annotations

import builtins
import hashlib

import pytest

from simulacra.config import THEMES_DIR, Settings
from simulacra.engine import dealings
from simulacra.engine.combat import (
    HIDDEN,
    SHAKEN,
    SHAKEN_PENALTY,
    actors_attack,
    best_weapon,
    player_attacks,
)
from simulacra.engine.events import Improvised, Line, Notice, ProseDelta, Roll, StatusChanged
from simulacra.engine.judge import Verdict, apply_verdict
from simulacra.engine.loop import Engine
from simulacra.engine.parser import infer, parse
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.narrate.narrator import Narrator
from simulacra.ui.transcript import TranscriptWriter
from simulacra.world import discovery
from simulacra.world.model import Actor, Item, RoomKind
from simulacra.world.theme import Theme

from conftest import FakeClient

TEXT = "A narrow shelf, its edge worn smooth by hands that are gone."
ARCHIVIST = "npc:archivist"


class Recorder(FakeClient):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.prompts: list[list[dict]] = []

    def stream(self, messages, policy, *, kind="stream"):
        self.prompts.append(list(messages))
        yield from super().stream(messages, policy, kind=kind)


@pytest.fixture
def game(tmp_path, theme):
    settings = Settings()
    store = Store(tmp_path / "w.db", world_seed=11)
    client = Recorder(script=[TEXT])
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator),
                    client=client)
    list(engine.begin())
    client.prompts.clear()
    client.calls.clear()
    client.stream_calls = 0  # begin() narrated the entrance; that is not the test
    yield engine, state, store, client
    store.close()


def hostile(name="a partial"):
    return Actor(id=f"mob:{name}", name=name, hp=5, max_hp=5)


def said(events) -> str:
    return "".join(e.text for e in events if isinstance(e, ProseDelta))


# -- taken items stayed in the description ---------------------------------


def test_the_prose_prompt_names_nothing_that_can_leave(game):
    engine, state, _, _ = game
    room = state.room
    room.items.append(Item(id="i:jar", name="a jar of clean water"))
    room.actors.append(hostile("an offcut"))

    census = engine.narrator._census(state.floor, room)
    assert "jar of clean water" not in census and "offcut" not in census
    assert "CONTAINS" not in census and "PRESENT" not in census


def test_the_cache_key_changed_with_the_prompt(game):
    """Worlds made before M11 hold prose written from a census that listed
    items. Without a new key, that prose -- and the jar -- would replay forever."""
    engine, state, _, _ = game
    floor, room, narrator = state.floor, state.room, engine.narrator
    digest = hashlib.sha1(
        f"{narrator._theme.name}|{room.concept}|{floor.theme_name}".encode()
    ).hexdigest()[:8]
    assert narrator._key(floor, room) != f"prose:{room.id}:{digest}"


def test_a_room_with_an_item_is_never_described_with_it(tmp_path, theme):
    settings = Settings()
    store = Store(tmp_path / "w.db", world_seed=11)
    client = Recorder(script=[TEXT])
    state = new_run(store, theme, settings, seed=1)
    state.room.items.append(Item(id="i:jar", name="a jar of clean water"))
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator),
                    client=client)
    list(engine.begin())

    prompts = "\n".join(m["content"] for p in client.prompts for m in p)
    assert "jar of clean water" not in prompts
    store.close()


# -- search did not add information ----------------------------------------


@pytest.mark.parametrize("text,verb,target", [
    ("search", "search", ""),
    ("search the shelves", "search", "shelves"),
    ("rummage", "search", ""),
    ("loot the offcut", "search", "offcut"),
    ("hide", "hide", ""),
    ("hide in the shadows", "hide", ""),
    ("equip knife", "equip", "knife"),
    ("wield the knife", "equip", "knife"),
    ("enter the annex", "enter", "annex"),
])
def test_the_verbs_the_playtest_reached_for_are_stage_one(text, verb, target):
    intent = parse(text)
    assert intent is not None, f"{text!r} still falls through to the model"
    assert (intent.verb, intent.target) == (verb, target)


def test_search_never_asks_the_parser_model(game, monkeypatch):
    engine, _, _, client = game
    monkeypatch.setattr(discovery, "is_fertile", lambda *a: True)
    list(engine.turn("search"))
    assert "tier1" not in client.calls


def names_what_it_is_asked(client):
    """A stand-in model whose find names its fixture, as M15 requires of one."""
    def stream(messages, policy, *, kind="stream"):
        client.prompts.append(list(messages))
        client.stream_calls += 1
        target = messages[-1]["content"].split("LOOKING AT: ", 1)[1].split("\n", 1)[0]
        yield f"{target[:1].upper()}{target[1:]}, its edge worn smooth by hands that are gone."
    client.stream = stream


def test_bare_search_finds_what_the_room_has_not_said(game, monkeypatch):
    engine, state, store, client = game
    monkeypatch.setattr(discovery, "is_fertile", lambda *a: True)
    names_what_it_is_asked(client)
    room = state.room

    list(engine.turn("search"))

    rows = store.canon(f"room:{room.id}")
    assert len(rows) == 1
    keys = set(store.node_data(f"room:{room.id}")["found"])
    said = discovery.words(room.name) | discovery.words(room.concept) | discovery.words(room.prose)
    pool = set().union(*(discovery.words(f) for f in engine.theme.fixture_names(room.kind)))
    assert keys <= pool, "bare search found something outside the room's fixtures"
    assert not keys & said, "bare search re-found something the room already said"


def test_bare_search_in_a_barren_room_costs_nothing(game, monkeypatch):
    engine, _, store, client = game
    monkeypatch.setattr(discovery, "is_fertile", lambda *a: False)

    events = list(engine.turn("search"))
    assert any(isinstance(e, Notice) and "nothing more" in e.text for e in events)
    assert client.stream_calls == 0


def test_the_same_room_gives_up_the_same_things(game, monkeypatch):
    engine, state, store, client = game
    monkeypatch.setattr(discovery, "is_fertile", lambda *a: True)
    names_what_it_is_asked(client)
    list(engine.turn("search"))
    first = set(store.node_data(f"room:{state.room.id}")["found"])

    store.retire_canon(f"room:{state.room.id}")
    data = store.node_data(f"room:{state.room.id}")
    data["found"] = {}
    store.set_node_data(f"room:{state.room.id}", data)
    list(engine.turn("search"))
    assert set(store.node_data(f"room:{state.room.id}")["found"]) == first


def test_the_room_itself_is_never_a_discovery(game):
    """All ten of the playtest's discoveries were keyed on the room's own name."""
    engine, state, store, client = game
    word = sorted(discovery.words(state.room.name), key=len)[-1]

    list(engine.turn(f"search the {word}"))
    assert store.canon(f"room:{state.room.id}") == []
    assert client.stream_calls == 0, "the room's own name generated something"


def test_looting_the_dead_is_honest(game):
    engine, _, _, client = game
    events = list(engine.turn("loot the offcut"))
    assert any(isinstance(e, Notice) and "nothing behind" in e.text for e in events)
    assert client.stream_calls == 0


def test_the_fallback_cannot_invent_a_look_target():
    """"search offcut", the offcut dead, became a look at the room's name."""
    client = FakeClient(structured_result={"verb": "look", "target": "lair of the forgotten"})
    intent = infer("poke at the offcut", "Lair of the Forgotten", client, Settings().intent)
    # M11.1: not a look with the target dropped -- that sent the request to the
    # room description. Nobody looked; it is an improvisation.
    assert intent.verb == "improvise"

    client = FakeClient(structured_result={"verb": "look", "target": "offcut"})
    intent = infer("poke at the offcut", "Lair of the Forgotten", client, Settings().intent)
    assert intent.target == "offcut"


# -- "how did I get braced?" -----------------------------------------------


def test_self_hindrance_is_called_shaken(game, monkeypatch):
    engine, state, _, _ = game
    monkeypatch.setattr("simulacra.engine.combat.attack_roll", lambda *a: (20, True))
    state.room.actors.append(hostile())
    apply_verdict(Verdict(plausible=True, difficulty=10, effect="status_self",
                          magnitude=2, reason="you slip"), state, state.rng)
    assert SHAKEN in state.player.effects
    assert "braced" not in state.player.effects


def test_shaken_lowers_the_defense_monsters_roll_against(game):
    _, state, _, _ = game
    player = state.player
    player.effects[SHAKEN] = 2
    rolls = [e for e in actors_attack([hostile()], player, state.rng) if isinstance(e, Roll)]
    assert rolls[0].target == player.defense - SHAKEN_PENALTY


def test_a_hidden_player_is_not_attacked(game):
    _, state, _, _ = game
    state.player.effects[HIDDEN] = 1
    assert actors_attack([hostile()], state.player, state.rng) == []


def test_hiding_needs_something_to_hide_from(game):
    engine, state, _, _ = game
    state.room.actors[:] = [a for a in state.room.actors if not a.hostile]
    events = list(engine.turn("hide"))
    assert any(isinstance(e, Notice) for e in events)
    assert not engine._resolved


def test_hiding_works_and_says_so_once(game, monkeypatch):
    engine, state, _, _ = game
    monkeypatch.setattr("simulacra.engine.loop.attack_roll", lambda *a: (20, True))
    state.room.actors.append(hostile())

    events = list(engine.turn("hide"))

    assert any(isinstance(e, Line) and "don't find you" in e.text for e in events)
    assert not any(isinstance(e, Roll) and "attacks" in e.label for e in events)
    statuses = [e for e in events if isinstance(e, StatusChanged)]
    assert len(statuses) == 1 and HIDDEN in statuses[0].effects


def test_you_cannot_hide_while_hidden(game, monkeypatch):
    engine, state, _, _ = game
    monkeypatch.setattr("simulacra.engine.loop.attack_roll", lambda *a: (20, True))
    state.room.actors.append(hostile())
    list(engine.turn("hide"))
    events = list(engine.turn("hide"))
    assert any(isinstance(e, Notice) and "already" in e.text for e in events)


def test_striking_from_hiding_gets_the_bonus_and_ends_it(game):
    _, state, _, _ = game
    state.player.effects[HIDDEN] = 1
    rolls = [e for e in player_attacks(state.player, hostile(), state.rng)
             if isinstance(e, Roll)]
    assert rolls[0].detail.endswith("from hiding")
    assert HIDDEN not in state.player.effects


def test_a_verdict_aimed_at_nothing_rolls_nothing(game):
    """"Jumping over the edge causes the enemy to stumble", in an empty room."""
    _, state, _, _ = game
    state.room.actors[:] = [a for a in state.room.actors if not a.hostile]
    events = apply_verdict(Verdict(plausible=True, difficulty=10, effect="status_target",
                                   magnitude=2, reason="the enemy stumbles"),
                           state, state.rng)
    assert not any(isinstance(e, Roll) for e in events)
    assert all(not e.plausible for e in events if isinstance(e, Improvised))
    assert not any("enemy" in e.reason for e in events if isinstance(e, Improvised))


def test_the_judge_is_told_when_nothing_is_hostile(game):
    engine, state, _, _ = game
    state.room.actors[:] = [a for a in state.room.actors if not a.hostile]
    assert "nothing hostile" in engine._room_summary()


# -- "can I switch weapons?" -----------------------------------------------


def test_equip_names_the_weapon_combat_actually_uses(game):
    engine, state, _, client = game
    knife = Item(id="i:k", name="a copyist's knife", damage=3)
    blade = Item(id="i:b", name="a stuttering blade", damage=4)
    state.player.inventory += [knife, blade]

    events = list(engine.turn("equip"))
    assert best_weapon(state.player) is blade
    assert any(isinstance(e, Line) and blade.name in e.text for e in events)

    events = list(engine.turn("equip the knife"))
    assert any(isinstance(e, Line) and "hits harder" in e.text for e in events)
    assert not engine._resolved
    assert "tier1" not in client.calls


def test_equip_with_empty_hands(game):
    engine, _, _, _ = game
    events = list(engine.turn("equip"))
    assert any(isinstance(e, Line) and "hands" in e.text for e in events)


def test_enter_a_neighbouring_room_by_name(game):
    engine, state, _, _ = game
    direction, dest = next(iter(state.room.exits.items()))
    state.floor.rooms[dest].name = "the annex"
    list(engine.turn("enter the annex"))
    assert state.room_id == dest


def test_enter_the_stairs_descends(game):
    engine, state, _, _ = game
    state.room_id = next(r.id for r in state.floor.rooms.values()
                         if r.kind is RoomKind.DESCENT)
    list(engine.turn("enter the stairs"))
    assert state.depth == 2


# -- "what's our logging / export situation?" ------------------------------


def test_a_transcript_reads_like_the_session(tmp_path, game):
    engine, state, _, _ = game
    path = tmp_path / "transcripts" / "w-run1.txt"
    writer = TranscriptWriter(path)
    writer.command("look")
    for event in engine.turn("look"):
        writer.handle(event)
    writer.close()

    text = path.read_text()
    assert "> look" in text
    assert state.room.name in text
    assert "\x1b" not in text, "escape codes leaked into the transcript"


def test_a_played_run_leaves_a_transcript(tmp_path, monkeypatch):
    from simulacra.__main__ import main

    lines = iter(["look", "quit"])
    monkeypatch.setattr(builtins, "input", lambda *_: next(lines))
    assert main(["--offline", "--db", str(tmp_path / "w.db"), "--seed", "3"]) == 0

    files = list((tmp_path / "transcripts").glob("w-*-run*.txt"))
    assert len(files) == 1
    assert "> look" in files[0].read_text()


def test_copying_in_the_tui_no_longer_quits_it():
    tui = pytest.importorskip("simulacra.ui.tui")
    bound = {(b.key, b.action) for b in tui.SimulacraApp.BINDINGS}
    assert ("ctrl+c", "leave") not in bound
    assert any(action == "leave" for _, action in bound), "there is no way to quit"


# -- closeout --------------------------------------------------------------


@pytest.fixture
def with_archivist(game):
    engine, state, store, client = game
    state.room_id = next(r.id for r in state.floor.rooms.values()
                         if any(a.id == ARCHIVIST for a in r.actors))
    return engine, state, store, client


def test_the_wary_band_has_a_door(with_archivist):
    engine, state, store, _ = with_archivist
    events = list(engine.turn("attack the archivist"))

    assert dealings.disposition(store, ARCHIVIST) == dealings.ATTACK_STEP
    assert dealings.is_wary(store, ARCHIVIST)
    assert not any(isinstance(e, Roll) for e in events), "an NPC was rolled against"
    npc = next(a for a in state.room.actors if a.id == ARCHIVIST)
    assert npc.hp == npc.max_hp


def test_a_bare_attack_never_starts_a_grudge(with_archivist):
    engine, state, store, _ = with_archivist
    state.room.actors[:] = [a for a in state.room.actors if not a.hostile]
    list(engine.turn("attack"))
    assert dealings.disposition(store, ARCHIVIST) == 0


def test_what_you_found_reaches_conversation(game):
    engine, state, store, _ = game
    store.add_canon(f"room:{state.room_id}", "A boot scraper, worn to a crescent.", "derived")
    texts = [f.text for f in engine._router._room(state, None, "here")]
    assert "A boot scraper, worn to a crescent." in texts


@pytest.mark.parametrize("pack", sorted(p.stem for p in THEMES_DIR.glob("*.toml")))
def test_every_theme_loads(pack):
    theme = Theme.load(pack)
    assert theme.pack == pack
    assert theme.npcs and all(n.canon for n in theme.npcs)
