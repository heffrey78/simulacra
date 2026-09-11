"""Settings: the one place a model is chosen, and every call site has to hear it.

Reported from a playtest: `--model qwen3.5:2b` warmed the 2b, then made every
real call on qwen3:1.7b. Nothing said so; `ollama ps` did.
"""

from __future__ import annotations

from argparse import Namespace

import simulacra.__main__ as entry
from simulacra.config import Settings
from simulacra.world.theme import Theme

from conftest import FakeClient

SITES = ("chat", "narrator", "judge", "intent", "director")


def names(settings) -> set[str]:
    return {getattr(settings, site).name for site in SITES}


def test_with_model_reaches_every_call_site():
    assert names(Settings().with_model("qwen3.5:2b")) == {"qwen3.5:2b"}


def test_with_model_keeps_each_sites_budget():
    before, after = Settings(), Settings().with_model("qwen3.5:2b")
    for site in SITES:
        b, a = getattr(before, site), getattr(after, site)
        assert (a.num_predict, a.temperature, a.num_ctx) == (b.num_predict, b.temperature, b.num_ctx)


def test_with_model_leaves_the_embedder_alone():
    assert Settings().with_model("qwen3.5:2b").embed == Settings().embed


def test_the_environment_variable_reaches_every_call_site(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMULACRA_MODEL", "qwen3.5:2b")
    assert names(Settings.load(tmp_path / "absent.toml")) == {"qwen3.5:2b"}


def test_the_model_flag_reaches_every_call_site(tmp_path, monkeypatch):
    """Through the real entry path, as the playtest ran it."""
    monkeypatch.setattr(entry, "OllamaClient", lambda settings: FakeClient())
    args = Namespace(offline=False, model="qwen3.5:2b", seed=1,
                     no_prefetch=True, no_canon=True)
    session = entry.build_session(args, Settings(db_path=tmp_path / "w.db"),
                                  Theme.load("simulacra"))
    try:
        assert names(session.settings) == {"qwen3.5:2b"}
        assert session.engine.narrator._policy.name == "qwen3.5:2b"
    finally:
        session.close()
