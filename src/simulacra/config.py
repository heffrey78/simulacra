"""Runtime configuration.

Every default here was chosen against a measured baseline: CPU-only inference at
roughly 10-15 tok/s (see plan.md, "Latency budget"). Loosen them only with a
benchmark in hand.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
THEMES_DIR = REPO_ROOT / "themes"
DEFAULT_DB = REPO_ROOT / "saves" / "world.db"


@dataclass(frozen=True)
class ModelPolicy:
    """One model, one calling convention.

    Policies are per *call site*, not per model: the narrator and the judge both
    run on the chat model but want wildly different budgets. Derive variants with
    `dataclasses.replace`.
    """

    name: str
    num_ctx: int = 4096
    num_predict: int = 256
    think: bool = False
    temperature: float = 0.8
    # Long keep_alive is load-bearing. A cold load costs 8-31s on this hardware,
    # which is longer than most turns; models must never fall out of memory.
    keep_alive: str = "60m"

    def with_(self, **kw) -> ModelPolicy:
        return replace(self, **kw)


@dataclass(frozen=True)
class Settings:
    host: str = "http://localhost:11434"
    db_path: Path = DEFAULT_DB
    theme: str = "simulacra"

    # One resident chat model. Swapping mid-game costs a full cold load, so the
    # narrator, judge and director all share this one.
    chat: ModelPolicy = field(
        default_factory=lambda: ModelPolicy(name="qwen3:1.7b", num_ctx=4096)
    )
    embed: ModelPolicy = field(
        default_factory=lambda: ModelPolicy(name="nomic-embed-text", num_ctx=2048)
    )

    # Per-call-site budgets. See plan.md for the tier definitions.
    narrator: ModelPolicy | None = None      # tier 2: streamed prose
    judge: ModelPolicy | None = None         # tier 1: small structured verdict
    intent: ModelPolicy | None = None        # tier 1: parser fallback
    director: ModelPolicy | None = None      # tier 3: once per floor

    prefetch_enabled: bool = True
    request_timeout: float = 120.0

    def __post_init__(self) -> None:
        # Derived policies, set once so call sites can rely on them being present.
        object.__setattr__(
            self, "narrator", self.narrator or self.chat.with_(num_predict=180, temperature=0.85)
        )
        object.__setattr__(
            self, "judge", self.judge or self.chat.with_(num_predict=120, temperature=0.3)
        )
        object.__setattr__(
            self, "intent", self.intent or self.chat.with_(num_predict=48, temperature=0.0)
        )
        object.__setattr__(
            self, "director", self.director or self.chat.with_(num_predict=400, temperature=0.9)
        )

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        """Load settings.toml if present, then let SIMULACRA_* env vars win."""
        data: dict = {}
        path = path or REPO_ROOT / "settings.toml"
        if path.exists():
            data = tomllib.loads(path.read_text())

        if host := os.environ.get("SIMULACRA_HOST"):
            data["host"] = host
        if theme := os.environ.get("SIMULACRA_THEME"):
            data["theme"] = theme
        if db := os.environ.get("SIMULACRA_DB"):
            data["db_path"] = db
        if model := os.environ.get("SIMULACRA_MODEL"):
            data["chat"] = {"name": model}

        chat = data.pop("chat", None)
        embed = data.pop("embed", None)
        kw: dict = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if "db_path" in kw:
            kw["db_path"] = Path(kw["db_path"])
        if chat:
            kw["chat"] = ModelPolicy(**chat)
        if embed:
            kw["embed"] = ModelPolicy(**embed)
        return cls(**kw)
