"""T1: what the NPC recalls follows what the player asked.

Assertions are on the string handed to `client.embed`, not on which memories
come back. A store with one death in it returns that death for any query
whatsoever, so retrieval results would pass whichever string was embedded --
only the embedded text proves recall is topic-aware.
"""

from __future__ import annotations

import pytest

from simulacra.config import Settings
from simulacra.engine.loop import Engine
from simulacra.engine.parser import split_address
from simulacra.engine.routes import DEATH_QUERY
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.narrate.narrator import Narrator

from conftest import FakeClient

ARCHIVIST = "npc:archivist"


class EmbedRecording(FakeClient):
    """Captures what was embedded. Only the `past` route embeds anything."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.embedded: list[str] = []

    def embed(self, texts, policy=None):
        self.embedded.extend(texts)
        return super().embed(texts, policy)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t1.db")
    yield s
    s.close()


def talk(store, theme, command: str) -> EmbedRecording:
    """Stand the player where the Archivist is and say one thing."""
    client = EmbedRecording()
    settings = Settings()
    state = new_run(store, theme, settings, seed=42)
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator),
                    client=client)
    for _ in engine.begin():
        pass
    host = next(r for r in state.floor.rooms.values()
                if any(a.id == ARCHIVIST for a in r.actors))
    state.room_id = host.id
    client.embedded.clear()  # begin() may embed; only the talk turn is the test
    for _ in engine.turn(command):
        pass
    return client


# -- the split itself ------------------------------------------------------

@pytest.mark.parametrize("target,addressee,topic", [
    ("archivist about arm", "archivist", "arm"),
    ("to archivist", "archivist", ""),
    ("archivist", "archivist", ""),
    ("", "", ""),
    # "about <npc>" is a real topic -- asking them about themselves.
    ("about archivist", "", "archivist"),
    # Split at the *first* connective, so the subject survives.
    ("archivist about archivist ledger", "archivist", "archivist ledger"),
    ("about brother", "", "brother"),
])
def test_the_parser_separates_address_from_subject(target, addressee, topic):
    """M8 moved this out of the talk handler: splitting a line is syntax, and
    doing it with three ordered stopword passes inside a verb handler meant
    every new social verb needed another pass."""
    assert split_address("talk", target) == (addressee, topic)


# -- through the engine ----------------------------------------------------

def test_a_question_embeds_the_question(store, theme):
    client = talk(store, theme, "ask archivist about the arm")
    assert client.embedded == ["arm"], client.embedded


def test_two_questions_embed_two_different_queries(store, theme):
    arm = talk(store, theme, "ask archivist about the arm")
    brother = talk(store, theme, "ask archivist about their brother")
    assert arm.embedded != brother.embedded
    assert DEATH_QUERY not in arm.embedded + brother.embedded


def test_greeting_still_volunteers_the_death(store, theme):
    """`talk to archivist` has a non-empty target -- the NPC's own name. It is
    not a topic, and treating it as one silently drops the volunteer-the-death
    behavior the milestone test depends on."""
    for command in ("talk to archivist", "talk", "talk to the archivist"):
        client = talk(store, theme, command)
        assert client.embedded == [DEATH_QUERY], (command, client.embedded)
