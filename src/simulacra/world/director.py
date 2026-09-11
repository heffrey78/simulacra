"""Tier-3: one call per floor that decides what the floor *is*.

This is the "hybrid by scale" split. `floorgen` has already produced a valid,
walkable graph. The director never sees it as a layout problem -- it receives a
room census and returns identity: a name, a goal, motifs, and one short concept
line per notable room. The narrator then expands those concepts into prose.

One ~400-token call per floor at ~15 tok/s is roughly 25-30 seconds. That is
affordable exactly once, hidden behind the descent beat (events.Thinking), and
only because every other tier is cheap.

**Corridors are excluded from the census.** They are over half a deep floor and
need no identity of their own, so asking for a concept per room would multiply
the most expensive call in the game by two for nothing.

MILESTONE M2.
"""

from __future__ import annotations

import re

from .model import Floor, RoomKind

FLOOR_SCHEMA = {
    "type": "object",
    "properties": {
        "theme_name": {"type": "string"},
        "goal": {"type": "string"},
        # Moods, not objects (M12). Code validates too; this is a hint.
        "motifs": {"type": "array", "maxItems": 3,
                   "items": {"type": "string", "maxLength": 24}},
        "rooms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "concept": {"type": "string"},
                },
                "required": ["id", "name", "concept"],
            },
        },
    },
    "required": ["theme_name", "goal", "motifs", "rooms"],
}

# Rooms worth an identity. Corridors keep their procedural names.
NOTABLE = {
    RoomKind.ENTRANCE, RoomKind.CHAMBER, RoomKind.VAULT,
    RoomKind.LAIR, RoomKind.SHRINE, RoomKind.DESCENT,
}

# Names the director is not allowed to impose. Asked for a room name, a 1.7b
# model very often just restates the structural role -- "Entrance", "Chamber",
# "Descent" -- which is strictly worse than the theme pack's own pools ("First
# Landing", "the Unworn Place"). Observed on the first live run of M2.
_GENERIC_NAMES = {k.value for k in RoomKind} | {
    "room", "hall", "hallway", "corridor", "passage", "stairs", "stairway",
    "the entrance", "the descent", "the vault", "the lair", "the shrine",
    "the chamber", "exit", "entry", "start", "end",
}


def _is_generic(name: str) -> bool:
    return name.strip().strip(".").lower() in _GENERIC_NAMES


# Room ids the director copies out of its own census into prose-bound text.
# Measured in the second playtest: "A rusted iron key lies in the wall of d1r0",
# reproduced by the narrator in four rooms.
_ID = re.compile(r"\b(?:(?:in|of|at|from|to|near|inside)\s+)?(?:the\s+)?(?:room\s+)?d\d+r\d+\b",
                 re.IGNORECASE)

# The director's stock vocabulary. Across both theme packs' worlds it named
# rooms "Chamber of Shadows", "Lair of the Forgotten", "Descent into the
# Unknown", "Shrine of the Lost" -- the same names in a copy of a vanished place
# and in a silver mine -- overwriting the theme's own far better pools.
_STOCK = frozenset({
    "forgotten", "unknown", "lost", "unseen", "shadow", "shadows", "void",
    "oblivion", "abyss", "eternal", "echo", "echoes", "vanished", "doom",
    "despair", "darkness", "whispers", "whispering", "hollowed", "dread",
})
_ROOM_HEADS = frozenset({
    "chamber", "hall", "room", "lair", "shrine", "vault", "descent", "entrance",
    "corridor", "passage", "cavern", "tunnel", "crypt", "sanctum", "den",
    "gateway", "portal",
})
_ARTICLES = frozenset({"the", "a", "an"})


def _clean(text: str) -> str:
    """Strip room ids, and the preposition that introduced them."""
    text = _ID.sub("", str(text or ""))
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r",\s*,", ",", text)
    return re.sub(r"\s{2,}", " ", text).strip(" ,;:")


# A trailing numeral is how the director dodges its own avoid-list: measured on
# the pre-M12 build, one world's floors were "Erebus's Veil", "Erebus's Veil II"
# and "Erebus's Veil III". Names are compared with these stripped.
_NUMBERED = re.compile(r"(?:\s+(?:[ivx]+|\d+))+$", re.IGNORECASE)


def _stem(name: str) -> str:
    stem = _NUMBERED.sub("", name.strip().lower()).strip(" .,:;")
    stem = re.sub(r"^(?:the|a|an)\s+", "", stem)
    return stem or name.strip().lower()


def _is_cliche(name: str) -> bool:
    """A stock abstraction anywhere, or a generic room word at the head."""
    words = re.findall(r"[a-z']+", name.lower())
    if not words:
        return True
    if set(words) & _STOCK:
        return True
    head = words[1] if words[0] in _ARTICLES and len(words) > 1 else words[0]
    return head in _ROOM_HEADS and len(words) > 1


def _mood(text: str) -> str:
    """A motif is a mood -- a word or two -- not an object in a room.

    The director wrote motifs as sentences about particular things ("A hollow
    statue in the lair"), and every room's prompt carried all of them, so the
    entrance described the lair's statue. A mood is short and has no article.
    """
    mood = _clean(text).lower().strip(" .")
    words = mood.split()
    if not words or len(words) > 3 or words[0] in _ARTICLES:
        return ""
    return mood


def _census(floor: Floor, theme) -> tuple[str, list[str]]:
    """Room ids with the *theme's* description of each role -- never the
    engine's own role words, which the model reads as altars and keys."""
    notable = [r for r in floor.rooms.values() if r.kind in NOTABLE]
    lines = [f"{r.id}: {theme.role(r.kind)}" for r in notable]
    return "\n".join(lines), [r.id for r in notable]


def direct_floor(
    floor, theme, client, policy, *,
    previously: list[str] = (), avoid_motifs: list[str] = (), used_names: list[str] = (),
) -> bool:
    """Fill in floor identity and per-room concepts, in place.

    `previously` names recent floors; `avoid_motifs` their moods. The model is
    told both, and code enforces both -- M12's playtests showed the telling
    alone does not work ("The Hollowed Depths" on every floor of one world).

    Non-fatal by contract: returns False on any failure and leaves the floor
    with its procedural names, so play continues -- just flatter.
    """
    census, ids = _census(floor, theme)
    avoid = ""
    if previously:
        avoid += ("\nRecent floors were: " + "; ".join(previously)
                  + ". Make this floor clearly different from those.")
    if avoid_motifs:
        avoid += "\nDo not reuse these moods: " + ", ".join(avoid_motifs) + "."

    messages = [
        {"role": "system", "content": theme.director_system or
         "You design one floor of a dungeon. Give it a specific identity. Be terse."},
        {"role": "user", "content":
            f"Depth {floor.depth}. Rooms:\n{census}{avoid}\n\n"
            "Name this floor and give it one concrete goal for the player. Give "
            "up to three moods for the whole floor -- one or two words each, a "
            "feeling, not an object and not a room. Then for EVERY room id "
            "above, a short name and a one-sentence concept. Use the room ids "
            "exactly as given, but never write them in any name or concept."},
    ]

    try:
        data = client.structured(messages, FLOOR_SCHEMA, policy, kind="tier3")
    except Exception:
        return False

    return apply_floor_plan(floor, data, valid_ids=set(ids), theme=theme,
                            used_names=used_names, used_motifs=avoid_motifs)


def apply_floor_plan(
    floor: Floor, data: dict, valid_ids: set[str] | None = None, *,
    theme=None, used_names=(), used_motifs=(),
) -> bool:
    """Apply a director response defensively.

    Split out from the call so it is testable without a model, and so a partly
    malformed response still contributes whatever it got right. A 1.7b model
    invents room ids; unknown ones are dropped rather than trusted. Since M12 it
    also refuses what the playtests showed it cannot stop producing: stock
    names, repeated floor names and moods, objects posing as moods, and room ids
    leaking into text a player will read.
    """
    if not isinstance(data, dict):
        return False

    name = _clean(str(data.get("theme_name") or ""))
    taken = {_stem(n) for n in used_names if n}
    if name and (_stem(name) in taken or _is_cliche(name)):
        name = ""
    if not name and theme is not None and (data.get("rooms") or data.get("theme_name")):
        name = theme.floor_title(floor.depth)
    if name:
        floor.theme_name = name
    floor.goal = _clean(str(data.get("goal") or ""))

    motifs = data.get("motifs") or []
    if isinstance(motifs, list):
        seen = {w for m in used_motifs for w in str(m).lower().split()}
        kept: list[str] = []
        for raw in motifs:
            mood = _mood(raw)
            if mood and not set(mood.split()) & seen and mood not in kept:
                kept.append(mood)
                seen |= set(mood.split())
        floor.motifs = tuple(kept[:3])

    applied = 0
    for entry in data.get("rooms") or []:
        if not isinstance(entry, dict):
            continue
        room_id = str(entry.get("id") or "").strip()
        if room_id not in floor.rooms:
            continue  # invented id
        if valid_ids is not None and room_id not in valid_ids:
            continue  # a corridor we deliberately didn't ask about
        room = floor.rooms[room_id]
        if concept := _clean(str(entry.get("concept") or "")):
            room.concept = concept
            applied += 1
        # The concept is the valuable half of the response; the name is only an
        # improvement when it says something the theme's own pool doesn't.
        name = _clean(str(entry.get("name") or ""))
        if name and not _is_generic(name) and not _is_cliche(name):
            room.name = name

    return bool(floor.theme_name) or applied > 0
