"""Theme pack loading.

The engine is setting-agnostic. Everything genre-shaped -- prose voice, room
names, monster rosters, banned words -- comes from `themes/<name>.toml`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..config import THEMES_DIR
from .model import RoomKind


@dataclass(frozen=True)
class Npc:
    anchor: str
    name: str
    role: str
    voice: str

    # Which floor this one lives on. Content, not structure -- which is why it
    # sits here next to their voice rather than as a constant in floorgen.
    #
    # Default 1 on purpose: the M3 depth distribution puts the median run's end
    # at floor 4, so an NPC placed deeper is one most runs never meet, and an
    # NPC nobody meets accumulates no canon. Persistent floors (M6) widen that a
    # little -- a world you can re-walk for free makes floor 3 reachable in a
    # way it was not -- but not without limit.
    depth: int = 1

    # What this NPC says when it has nothing, and when it has already said it.
    # These are spoken *without a model call* -- see routes.Brief.spoken. A 1.7b
    # told "you do not know anything about that" invents an answer anyway
    # (measured: the Archivist explained that the Corrector is "a device used to
    # alter records"), so refusing is code's job and only the wording is the
    # theme's. Blank falls back to the engine default.
    refusal: str = ""
    repeat: str = ""

    # Seed canon: `provenance='authored'`. The one class of canon that is never
    # generated and never mutated, and the only input (with episodic memory)
    # that derived canon is allowed to be built from.
    canon: tuple[str, ...] = ()


# What the model is told a room *is*, when the theme pack doesn't say (M12).
#
# `RoomKind` is structure and stays the engine's. Its *values* -- "shrine",
# "vault", "lair" -- were going to the director and the narrator verbatim, and
# they are fantasy-dungeon words: told "shrine", a small model wrote altars and
# statues of gods into a silver mine. These describe the role without a setting.
ROLE_DEFAULTS: dict[RoomKind, str] = {
    RoomKind.ENTRANCE: "where this floor begins",
    RoomKind.CORRIDOR: "a way between rooms",
    RoomKind.CHAMBER: "a large working space",
    RoomKind.VAULT: "where something worth having is kept",
    RoomKind.LAIR: "where something dangerous has settled",
    RoomKind.SHRINE: "the floor's most remarkable place",
    RoomKind.DESCENT: "where the way down is",
}

_ORDINALS = ("First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh",
             "Eighth", "Ninth", "Tenth", "Eleventh", "Twelfth")


@dataclass
class Theme:
    name: str
    # What `--theme` accepts and what a world records: the pack's file stem.
    # `name` is the display name from the TOML, and the two differ ("simulacra"
    # vs "Simulacra") -- storing the wrong one makes the world's own error
    # message suggest a `--theme` argument that does not resolve.
    pack: str = ""
    tagline: str = ""
    narrator_system: str = ""
    director_system: str = ""
    judge_system: str = ""
    motifs: tuple[str, ...] = ()
    banned: tuple[str, ...] = ()
    rooms: dict[str, list[str]] = field(default_factory=dict)
    monsters: dict[str, list[list]] = field(default_factory=dict)
    items: dict[str, list[list]] = field(default_factory=dict)
    # Things plausibly found in a room of each kind, beyond whatever the
    # director happened to mention. Structure is code's; names are the
    # theme's -- the same rule as `rooms` and `monsters`.
    fixtures: dict[str, list[str]] = field(default_factory=dict)
    npcs: list[Npc] = field(default_factory=list)
    # The theme's own words for each room role (M12). Optional: ROLE_DEFAULTS
    # already keep engine vocabulary out of the prompts; this is voice.
    roles: dict[str, str] = field(default_factory=dict)
    # What a floor is called when the director's name is refused -- repeated,
    # or a stock phrase. `{n}` is the depth, `{nth}` its ordinal word.
    floor_name: str = ""

    def role(self, kind: RoomKind) -> str:
        return (self.roles.get(kind.value) or "").strip() or ROLE_DEFAULTS[kind]

    def floor_title(self, depth: int) -> str:
        nth = _ORDINALS[depth - 1] if 0 < depth <= len(_ORDINALS) else f"{depth}th"
        return (self.floor_name or "Floor {n}").format(n=depth, nth=nth)

    def room_names(self, kind: RoomKind) -> list[str]:
        return self.rooms.get(kind.value) or [kind.value]

    def fixture_names(self, kind: RoomKind) -> list[str]:
        return self.fixtures.get(kind.value) or []

    def style_note(self, *, motifs: bool = True) -> str:
        """Compact style rider appended to generation prompts.

        `motifs=False` for dialogue. A comma-separated motif list reads to a
        small model as a form to fill in: the Archivist's first live reply was
        the motif list with "1234567890" against every entry. Motifs are
        scene-dressing vocabulary for description, not for speech.
        """
        parts = []
        if motifs and self.motifs:
            parts.append("Motifs: " + ", ".join(self.motifs) + ".")
        if self.banned:
            parts.append("Never use these words or phrases: " + ", ".join(self.banned) + ".")
        return " ".join(parts)

    @classmethod
    def load(cls, name: str, themes_dir: Path | None = None) -> Theme:
        path = (themes_dir or THEMES_DIR) / f"{name}.toml"
        if not path.exists():
            available = sorted(p.stem for p in (themes_dir or THEMES_DIR).glob("*.toml"))
            raise FileNotFoundError(f"no theme {name!r}; available: {available}")
        d = tomllib.loads(path.read_text())
        voice = d.get("voice", {})
        return cls(
            name=d.get("name", name),
            pack=name,
            tagline=d.get("tagline", ""),
            narrator_system=voice.get("system", ""),
            director_system=d.get("director", {}).get("system", ""),
            judge_system=d.get("judge", {}).get("system", ""),
            motifs=tuple(voice.get("motifs", [])),
            banned=tuple(voice.get("banned", [])),
            rooms=d.get("rooms", {}),
            monsters=d.get("monsters", {}),
            items=d.get("items", {}),
            fixtures=d.get("fixtures", {}),
            roles=d.get("roles", {}),
            floor_name=d.get("director", {}).get("floor_name", ""),
            npcs=[
                Npc(
                    anchor=n["anchor"], name=n["name"], role=n["role"], voice=n["voice"],
                    depth=int(n.get("depth", 1)),
                    refusal=n.get("refusal", ""), repeat=n.get("repeat", ""),
                    canon=tuple(n.get("canon", ())),
                )
                for n in d.get("npcs", {}).get("roster", [])
            ],
        )
