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

from .model import Floor, RoomKind

FLOOR_SCHEMA = {
    "type": "object",
    "properties": {
        "theme_name": {"type": "string"},
        "goal": {"type": "string"},
        "motifs": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
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


def _census(floor: Floor) -> tuple[str, list[str]]:
    notable = [r for r in floor.rooms.values() if r.kind in NOTABLE]
    lines = [f"{r.id} is a {r.kind.value}" for r in notable]
    return "\n".join(lines), [r.id for r in notable]


def direct_floor(floor, theme, client, policy, *, previously: list[str] = ()) -> bool:
    """Fill in floor identity and per-room concepts, in place.

    `previously` carries the last few floors' theme names so the model is told
    what NOT to repeat -- small models converge hard on one idea otherwise.

    Non-fatal by contract: returns False on any failure and leaves the floor
    with its procedural names, so play continues -- just flatter.
    """
    census, ids = _census(floor)
    avoid = ""
    if previously:
        avoid = (
            "\nRecent floors were: " + "; ".join(previously)
            + ". Make this floor clearly different from those."
        )

    messages = [
        {"role": "system", "content": theme.director_system or
         "You design one floor of a dungeon. Give it a specific identity. Be terse."},
        {"role": "user", "content":
            f"Depth {floor.depth}. Rooms:\n{census}{avoid}\n\n"
            "Name this floor, give it one concrete goal for the player, up to "
            "three motifs, and for EVERY room id above a short name and a "
            "one-sentence concept. Use the room ids exactly as given."},
    ]

    try:
        data = client.structured(messages, FLOOR_SCHEMA, policy, kind="tier3")
    except Exception:
        return False

    return apply_floor_plan(floor, data, valid_ids=set(ids))


def apply_floor_plan(floor: Floor, data: dict, valid_ids: set[str] | None = None) -> bool:
    """Apply a director response defensively.

    Split out from the call so it is testable without a model, and so a partly
    malformed response still contributes whatever it got right. A 1.7b model
    invents room ids; unknown ones are dropped rather than trusted.
    """
    if not isinstance(data, dict):
        return False

    if name := str(data.get("theme_name") or "").strip():
        floor.theme_name = name
    floor.goal = str(data.get("goal") or "").strip()

    motifs = data.get("motifs") or []
    if isinstance(motifs, list):
        floor.motifs = tuple(str(m).strip() for m in motifs[:3] if str(m).strip())

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
        if concept := str(entry.get("concept") or "").strip():
            room.concept = concept
            applied += 1
        # The concept is the valuable half of the response; the name is only an
        # improvement when it actually says something.
        if (name := str(entry.get("name") or "").strip()) and not _is_generic(name):
            room.name = name

    return bool(floor.theme_name) or applied > 0
