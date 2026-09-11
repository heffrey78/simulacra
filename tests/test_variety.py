"""M12: a floor's rooms read as different rooms, and a mine reads as a mine.

Both 2026-09-10 playtests. The mechanism is the engine's -- Hardpan proved it --
so these tests are about what reaches the prompts, not what the model writes.
The model's side is measured live (docs/tasks/M12-variety-tasks.md, V5).
"""

from __future__ import annotations

import hashlib
import random

import pytest

from simulacra.config import Settings
from simulacra.engine.events import Notice
from simulacra.engine.loop import Engine
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.narrate.narrator import Narrator
from simulacra.world.director import apply_floor_plan, direct_floor
from simulacra.world.floorgen import generate_floor
from simulacra.world.model import RoomKind
from simulacra.world.theme import ROLE_DEFAULTS, Theme

from conftest import FakeClient

THEMES = ["simulacra", "hardpan"]
ROLE_WORDS = {k.value for k in RoomKind}


def floor_of(theme, depth=1, seed=4):
    return generate_floor(depth, theme, random.Random(seed))


def narrator_for(theme, tmp_path):
    return Narrator(FakeClient(), theme, Store(tmp_path / "n.db"), Settings().narrator)


# -- V1: roles in the theme's words ------------------------------------------


@pytest.mark.parametrize("pack", THEMES)
def test_no_engine_role_word_reaches_the_narrator(pack, tmp_path):
    """Told "shrine", the model wrote altars and statues of gods into a mine."""
    theme = Theme.load(pack)
    floor = floor_of(theme)
    narrator = narrator_for(theme, tmp_path)
    for room in floor.rooms.values():
        role = next(line for line in narrator._census(floor, room).splitlines()
                    if line.startswith("ROLE:"))
        assert not set(role.lower().replace("role:", "").split()) & ROLE_WORDS, role


@pytest.mark.parametrize("pack", THEMES)
def test_no_engine_role_word_reaches_the_director(pack):
    theme = Theme.load(pack)
    prompts = []

    class Recorder(FakeClient):
        def structured(self, messages, schema, policy, *, kind="structured", retries=1):
            prompts.append(messages)
            return None

    direct_floor(floor_of(theme), theme, Recorder(), Settings().director)
    census = prompts[0][1]["content"].split("Rooms:\n", 1)[1].split("\n\n", 1)[0]
    for line in census.splitlines():
        assert not set(line.split(": ", 1)[1].lower().split()) & ROLE_WORDS, line


def test_the_neutral_defaults_are_themselves_neutral():
    for kind, text in ROLE_DEFAULTS.items():
        assert not set(text.lower().split()) & ROLE_WORDS, (kind, text)


def test_a_theme_can_voice_its_own_roles():
    theme = Theme.load("simulacra")
    assert theme.role(RoomKind.SHRINE) == "the part the copy got most nearly right"
    assert Theme.load("hardpan").role(RoomKind.SHRINE) == ROLE_DEFAULTS[RoomKind.SHRINE]


# -- V2: one mood per room ---------------------------------------------------


@pytest.mark.parametrize("pack", THEMES)
def test_each_room_carries_one_mood_and_never_the_list(pack, tmp_path):
    theme = Theme.load(pack)
    floor = floor_of(theme)
    floor.motifs = ("dust", "debt", "thirst")
    narrator = narrator_for(theme, tmp_path)
    for room in floor.rooms.values():
        census = narrator._census(floor, room)
        assert census.count("MOOD:") == 1
        assert "MOTIFS" not in census


def test_moods_rotate_across_a_floor(tmp_path):
    """Run 2 of the second playtest put all three motifs in every room."""
    theme = Theme.load("hardpan")
    floor = floor_of(theme)
    floor.motifs = ("dust", "debt", "thirst")
    narrator = narrator_for(theme, tmp_path)
    moods = {narrator._mood(floor, r) for r in floor.rooms.values()}
    assert moods == {"dust", "debt", "thirst"}


def test_no_motif_list_in_the_narrators_system_prompt(tmp_path):
    """`tallow` appeared 24 times in the second playtest's transcript."""
    theme = Theme.load("hardpan")
    system = narrator_for(theme, tmp_path)._system()
    assert "Motifs:" not in system
    assert "tallow" not in system
    assert theme.banned[0] in system, "the banned list still has to reach it"


def test_the_prose_cache_regenerates_for_the_new_prompt(tmp_path):
    theme = Theme.load("simulacra")
    floor = floor_of(theme)
    room = floor.room(floor.entrance_id)
    narrator = narrator_for(theme, tmp_path)
    m11 = hashlib.sha1(
        f"2|{theme.name}|{room.concept}|{floor.theme_name}".encode()
    ).hexdigest()[:8]
    assert narrator._key(floor, room) != f"prose:{room.id}:{m11}"


# -- V2/V3: what the director is allowed to write ----------------------------


def plan(floor, **over):
    rooms = [{"id": r.id, "name": "The Veil's Eye", "concept": "A low room."}
             for r in floor.rooms.values() if r.kind is not RoomKind.CORRIDOR]
    return {"theme_name": "The Drowned Stacks", "goal": "Find the plate.",
            "motifs": ["dust"], "rooms": rooms, **over}


def test_objects_posing_as_moods_are_dropped():
    """"A hollow statue in the lair" in every room's prompt made the entrance
    describe the lair's statue."""
    theme = Theme.load("hardpan")
    floor = floor_of(theme)
    apply_floor_plan(floor, plan(floor, motifs=[
        "A rusted iron key lies in the wall of d1r0",
        "A hollow statue in the lair",
        "Shattered Mirror",
        "dust",
    ]), theme=theme)
    assert floor.motifs == ("shattered mirror", "dust")


def test_room_ids_never_reach_player_text():
    """"A rusted iron key lies in the wall of d1r0", reproduced in four rooms."""
    theme = Theme.load("hardpan")
    floor = floor_of(theme)
    rid = next(r.id for r in floor.rooms.values() if r.kind is RoomKind.SHRINE)
    apply_floor_plan(floor, {"theme_name": "Deep Workings", "goal": f"Reach {rid}.",
                             "motifs": [], "rooms": [
                                 {"id": rid, "name": "The Wet Face",
                                  "concept": f"A key lies in the wall of {rid}, rusted."}]},
                     theme=theme)
    text = floor.goal + " " + floor.rooms[rid].concept
    assert rid not in text
    assert "A key lies in the wall, rusted." == floor.rooms[rid].concept


@pytest.mark.parametrize("name", [
    "Chamber of Shadows", "Lair of the Forgotten", "Descent into the Unknown",
    "Shrine of the Lost", "Entrance Hall", "Portal to the Lost", "Vault of the Unseen",
])
def test_the_directors_stock_names_are_refused(name):
    """The same names turned up in both themes' worlds, overwriting the
    theme's own pools."""
    theme = Theme.load("hardpan")
    floor = floor_of(theme)
    before = {r.id: r.name for r in floor.rooms.values()}
    apply_floor_plan(floor, plan(floor, rooms=[
        {"id": r.id, "name": name, "concept": "A low room."}
        for r in floor.rooms.values() if r.kind is not RoomKind.CORRIDOR
    ]), theme=theme)
    assert all(r.name == before[r.id] for r in floor.rooms.values())


def test_a_name_that_says_something_is_kept():
    theme = Theme.load("hardpan")
    floor = floor_of(theme)
    apply_floor_plan(floor, plan(floor), theme=theme)
    assert any(r.name == "The Veil's Eye" for r in floor.rooms.values())


def test_a_repeated_floor_name_is_refused():
    """"The Hollowed Depths" named every floor of one world."""
    theme = Theme.load("simulacra")
    floor = floor_of(theme, depth=2)
    apply_floor_plan(floor, plan(floor), theme=theme, used_names=["The Drowned Stacks"])
    assert floor.theme_name == "The Second Impression"


def test_a_stock_floor_name_is_refused():
    theme = Theme.load("hardpan")
    floor = floor_of(theme, depth=3)
    apply_floor_plan(floor, plan(floor, theme_name="Echoes of the Forgotten"), theme=theme)
    assert floor.theme_name == "The Third Level"


def test_a_repeated_mood_is_refused():
    """All five floors of the first playtest's world had a mirror."""
    theme = Theme.load("simulacra")
    floor = floor_of(theme, depth=2)
    apply_floor_plan(floor, plan(floor, motifs=["cracked mirror", "ink", "thirst"]),
                     theme=theme, used_motifs=["shattered mirror"])
    assert "cracked mirror" not in floor.motifs
    assert floor.motifs == ("ink", "thirst")


def test_the_director_is_told_the_moods_to_avoid():
    theme = Theme.load("simulacra")
    prompts = []

    class Recorder(FakeClient):
        def structured(self, messages, schema, policy, *, kind="structured", retries=1):
            prompts.append(messages)
            return None

    direct_floor(floor_of(theme), theme, Recorder(), Settings().director,
                 avoid_motifs=["shattered mirror"])
    assert "shattered mirror" in prompts[0][1]["content"]


def test_the_engine_passes_every_other_floors_names_and_moods(tmp_path, theme):
    settings = Settings()
    store = Store(tmp_path / "w.db", world_seed=5)
    client = FakeClient(script=["A room."], structured_result={
        "theme_name": "The Drowned Stacks", "goal": "", "motifs": ["ink"], "rooms": []})
    state = new_run(store, theme, settings, seed=5)
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator), client=client)
    list(engine.begin())
    state.room_id = next(r.id for r in state.floor.rooms.values()
                         if r.kind is RoomKind.DESCENT)
    list(engine.descend())

    assert state.floor.theme_name == theme.floor_title(2), "floor 2 repeated floor 1's name"
    assert "ink" not in state.floor.motifs
    store.close()


def test_floor_titles():
    assert Theme.load("simulacra").floor_title(2) == "The Second Impression"
    assert Theme.load("hardpan").floor_title(4) == "The Fourth Level"
    # The engine's own fallback, for a theme with no pattern.
    from dataclasses import replace
    assert replace(Theme.load("hardpan"), floor_name="").floor_title(4) == "Floor 4"


# -- V4: a described thing you can't take ------------------------------------


def test_taking_part_of_the_room_explains_itself(tmp_path, theme):
    """"A rusted iron key lies in the wall" -> "There is no key here." x3."""
    settings = Settings()
    store = Store(tmp_path / "w.db")
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme)
    list(engine.begin())
    state.room.prose = "A rusted iron key lies in the wall, its teeth worn flat."

    events = list(engine.turn("take key"))
    assert any(isinstance(e, Notice) and "part of the room" in e.text for e in events)
    events = list(engine.turn("take chandelier"))
    assert any(isinstance(e, Notice) and "There is no chandelier" in e.text for e in events)
    store.close()


@pytest.mark.parametrize("dodge", ["Erebus's Veil II", "Erebus's Veil III",
                                   "The Erebus's Veil 2", "erebus's veil"])
def test_a_numbered_repeat_is_still_a_repeat(dodge):
    """Measured on the pre-M12 build: "Erebus's Veil", "Erebus's Veil II",
    "Erebus's Veil III" -- the director's way round its own avoid-list."""
    theme = Theme.load("simulacra")
    floor = floor_of(theme, depth=2)
    apply_floor_plan(floor, plan(floor, theme_name=dodge), theme=theme,
                     used_names=["Erebus's Veil"])
    assert floor.theme_name == "The Second Impression"


def test_an_old_worlds_object_motifs_are_filtered_on_read(tmp_path):
    """Worlds made before M12 stored motifs like "A hollow statue in the lair".
    The mood rule ran only on the director's output, so they came back from the
    graph as one-per-room moods -- still objects, still in the wrong rooms."""
    theme = Theme.load("hardpan")
    floor = floor_of(theme)
    narrator = narrator_for(theme, tmp_path)

    floor.motifs = ("A rusted ladder leading to the entrance", "A hollow statue in the lair",
                    "damp")
    assert {narrator._mood(floor, r) for r in floor.rooms.values()} == {"damp"}

    floor.motifs = ("A hollow statue in the lair",)
    assert all(narrator._mood(floor, r) in theme.motifs for r in floor.rooms.values())
