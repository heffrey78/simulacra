"""The event seam (plan.md §6) enforced mechanically.

Cheap to write, and the only thing that will actually stop the seam eroding
under time pressure. If this fails, the fix is to emit an Event -- not to add an
exception here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "simulacra"

# Layers that must never know how they are displayed.
HEADLESS = ["engine", "world", "memory"]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Resolve relative imports to a dotted path we can reason about.
            if node.level:
                parts = path.relative_to(SRC).parts[:-1]
                base = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                found.add(".".join([*base, node.module or ""]).strip("."))
            elif node.module:
                found.add(node.module)
    return found


def _modules(layer: str) -> list[Path]:
    return sorted((SRC / layer).glob("*.py"))


@pytest.mark.parametrize("path", [p for layer in HEADLESS for p in _modules(layer)],
                         ids=lambda p: f"{p.parent.name}/{p.name}")
def test_headless_layers_never_import_ui(path):
    offenders = {m for m in _imports(path) if m.startswith("ui") or ".ui" in m}
    assert not offenders, f"{path.name} imports the UI layer: {offenders}"


@pytest.mark.parametrize("path", [p for layer in HEADLESS for p in _modules(layer)],
                         ids=lambda p: f"{p.parent.name}/{p.name}")
def test_headless_layers_never_print(path):
    """A stray print() bypasses the renderer and cannot be captured by a TUI."""
    tree = ast.parse(path.read_text())
    prints = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
    ]
    assert not prints, f"{path.name} calls print() at lines {prints}"


def test_room_text_is_produced_in_exactly_one_place():
    """`_describe` is the M2 swap point. A second producer of room prose means
    the narrator swap stops being a one-function change."""
    tree = ast.parse((SRC / "engine" / "loop.py").read_text())
    engine = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.ClassDef) and n.name == "Engine")
    callers = {
        fn.name
        for fn in engine.body
        if isinstance(fn, ast.FunctionDef)
        for n in ast.walk(fn)
        if isinstance(n, ast.Attribute) and n.attr == "_describe"
    }
    assert callers <= {"_enter_room", "_look"}, f"unexpected _describe callers: {callers}"
