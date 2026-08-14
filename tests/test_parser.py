"""Stage-1 parser. Pure function, so this is a table."""

from __future__ import annotations

import pytest

from simulacra.engine.parser import VERB_ALIASES, infer, parse

from conftest import FakeClient


@pytest.mark.parametrize(
    ("text", "verb", "target"),
    [
        # Bare directions -- the most common input in the game.
        ("n", "move", "north"), ("north", "move", "north"),
        ("s", "move", "south"), ("e", "move", "east"), ("w", "move", "west"),
        ("u", "move", "up"), ("d", "move", "down"),
        # Verb + direction.
        ("go north", "move", "north"), ("walk s", "move", "south"),
        ("head east", "move", "east"),
        # Articles are stripped so both spellings agree.
        ("take lamp", "take", "lamp"), ("take the lamp", "take", "lamp"),
        ("get a jar", "take", "jar"), ("grab an offcut", "take", "offcut"),
        # Multi-word verbs beat their single-word prefixes.
        ("pick up lamp", "take", "lamp"),
        ("talk to the archivist", "talk", "archivist"),
        # Intransitives ignore trailing words rather than failing.
        ("look", "look", ""), ("look around", "look", ""), ("l", "look", ""),
        ("look here", "look", ""), ("look about", "look", ""),
        # But looking at something specific keeps the target.
        ("look at the offcut", "look", "offcut"), ("examine offcut", "look", "offcut"),
        ("x lamp", "look", "lamp"), ("inspect the ration", "look", "ration"),
        ("inventory", "inventory", ""), ("i", "inventory", ""),
        ("wait", "wait", ""), ("z", "wait", ""),
        ("quit", "quit", ""), ("q", "quit", ""), ("exit", "quit", ""),
        ("descend", "descend", ""), ("stairs", "descend", ""),
        # Attack.
        ("attack ghoul", "attack", "ghoul"), ("kill the duplicate", "attack", "duplicate"),
        ("punch offcut", "attack", "offcut"), ("kick the offcut", "attack", "offcut"),
        ("karate chop offcut", "attack", "offcut"), ("drop kick offcut", "attack", "offcut"),
        # Normalisation.
        ("  TAKE   The   Lamp  ", "take", "lamp"),
    ],
)
def test_parses(text, verb, target):
    intent = parse(text)
    assert intent is not None, f"{text!r} should parse"
    assert (intent.verb, intent.target) == (verb, target)


@pytest.mark.parametrize("text", ["", "   ", "\n", "xyzzy", "flood the room with water",
                                  "go to the well", "sing a song"])
def test_returns_none_rather_than_raising(text):
    # None is the M3 escalation signal. It must never be an exception.
    assert parse(text) is None


def test_bare_go_is_a_parse_success_not_a_failure():
    # "go" is a known verb missing an argument; the engine asks "Go where?".
    intent = parse("go")
    assert intent is not None
    assert intent.verb == "move" and intent.target == ""


def test_raw_is_always_the_normalised_input():
    # M3's stage 2 needs the original text to send to the model.
    assert parse("  TAKE  The  Lamp ").raw == "take the lamp"


@pytest.mark.parametrize("alias", sorted(VERB_ALIASES))
def test_every_alias_in_the_table_parses(alias):
    # Guards against adding an alias that a longer entry silently shadows.
    intent = parse(alias if alias not in {"go", "walk", "head", "move"} else f"{alias} north")
    assert intent is not None, f"alias {alias!r} does not parse"
    assert intent.verb == VERB_ALIASES[alias]


# -- stage 2: the LLM fallback ----------------------------------------------


def test_infer_distrusts_a_take_target_the_player_never_typed():
    """A live session had 'karate chop offuct' (a typo) classified as take with
    a target lifted from the room census ('ration') rather than the input --
    silently taking an unrelated item. The target must be grounded in what the
    player actually typed."""
    client = FakeClient(structured_result={"verb": "take", "target": "ration"})
    intent = infer("karate chop offuct", "an unspoiled ration is here.", client, None)
    assert intent.verb == "improvise"


def test_infer_trusts_a_take_target_the_player_did_type():
    client = FakeClient(structured_result={"verb": "take", "target": "lamp"})
    intent = infer("grab that lamp", "a lamp is here.", client, None)
    assert intent.verb == "take" and intent.target == "lamp"


def test_infer_distrusts_a_use_target_the_player_never_typed():
    client = FakeClient(structured_result={"verb": "use", "target": "potion"})
    intent = infer("down it", "a potion is in your pack.", client, None)
    assert intent.verb == "improvise"
