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


@dataclass
class Theme:
    name: str
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
            tagline=d.get("tagline", ""),
            narrator_system=voice.get("system", ""),
            director_system=d.get("director", {}).get("system", ""),
            judge_system=d.get("judge", {}).get("system", ""),
            motifs=tuple(voice.get("motifs", [])),
            banned=tuple(voice.get("banned", [])),
            rooms=d.get("rooms", {}),
            monsters=d.get("monsters", {}),
            items=d.get("items", {}),
            npcs=[Npc(**n) for n in d.get("npcs", {}).get("roster", [])],
        )
