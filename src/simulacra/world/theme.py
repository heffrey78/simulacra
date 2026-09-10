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

    # Seed canon: `provenance='authored'`. The one class of canon that is never
    # generated and never mutated, and the only input (with episodic memory)
    # that derived canon is allowed to be built from.
    canon: tuple[str, ...] = ()


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
    npcs: list[Npc] = field(default_factory=list)

    def room_names(self, kind: RoomKind) -> list[str]:
        return self.rooms.get(kind.value) or [kind.value]

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
            npcs=[
                Npc(
                    anchor=n["anchor"], name=n["name"], role=n["role"], voice=n["voice"],
                    depth=int(n.get("depth", 1)),
                    canon=tuple(n.get("canon", ())),
                )
                for n in d.get("npcs", {}).get("roster", [])
            ],
        )
