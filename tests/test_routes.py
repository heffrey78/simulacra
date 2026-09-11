"""M8: retrieval routes and the conversation session.

The efficiency claim is that six of seven routes never embed anything, and
`test_no_resolver_but_past_touches_the_model` is the only thing keeping it true.
The quality claim is that a route resolving to nothing *is* "I don't know" —
which is why there is no threshold anywhere in this file to calibrate.
"""

from __future__ import annotations

import pytest

from simulacra.config import Settings
from simulacra.engine.events import Notice, ProseDelta, RunEnded, Transcript
from simulacra.engine.loop import Conversation, Engine
from simulacra.engine.routes import (
    DEATH_QUERY,
    DEFAULT_REFUSAL,
    DEFAULT_REPEAT,
    Fact,
    Router,
)
from simulacra.engine.state import new_run
from simulacra.memory.store import Store
from simulacra.narrate.narrator import Narrator
from simulacra.world.model import Actor, Item

from conftest import FakeClient

REPLY = "Two hundred and nine went down."
ARCHIVIST = "npc:archivist"


class Recorder(FakeClient):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.prompts: list[list[dict]] = []
        self.embedded: list[str] = []
        self.structured_calls = 0

    def stream(self, messages, policy, *, kind="stream"):
        self.prompts.append(list(messages))
        yield from super().stream(messages, policy, kind=kind)

    def structured(self, messages, schema, policy, *, kind="structured", retries=1):
        self.structured_calls += 1
        self.prompts.append(list(messages))
        return super().structured(messages, schema, policy, kind=kind, retries=retries)

    def embed(self, texts, policy=None):
        self.embedded.extend(texts)
        return super().embed(texts, policy)


def prompt_text(client) -> str:
    return "\n".join(m["content"] for m in client.prompts[-1])


@pytest.fixture
def talking(tmp_path, theme):
    """The player, standing where the Archivist is, in a fresh world."""
    settings = Settings()
    store = Store(tmp_path / "w.db")
    client = Recorder(script=[REPLY])
    state = new_run(store, theme, settings, seed=1)
    engine = Engine(state, store, settings, theme,
                    narrator=Narrator(client, theme, store, settings.narrator),
                    client=client)
    list(engine.begin())
    host = next(r for r in state.floor.rooms.values()
                if any(a.id == ARCHIVIST for a in r.actors))
    state.room_id = host.id
    client.prompts.clear()
    client.embedded.clear()
    yield engine, state, store, client
    store.close()


def actor_of(state):
    return next(a for a in state.room.actors if a.id == ARCHIVIST)


def refusal_of(theme, anchor):
    npc = next(n for n in theme.npcs if n.anchor == anchor)
    return npc.refusal or DEFAULT_REFUSAL


def repeat_of(theme, anchor):
    npc = next(n for n in theme.npcs if n.anchor == anchor)
    return npc.repeat or DEFAULT_REPEAT


# -- R3: classification ----------------------------------------------------


@pytest.mark.parametrize("topic,route", [
    ("the corridor", "room"),
    ("what is nearby", "monsters"),
    ("the blade", "item"),
    ("your name", "self"),
    ("the last delver", "past"),
    ("the corrector", "npc"),
])
def test_stage_one_routes_without_a_model_call(tmp_path, theme, topic, route):
    store = Store(tmp_path / "w.db")
    client = Recorder()
    router = Router(theme, store, Settings(), client=client)

    assert router.classify(topic) == (route, False)
    assert client.structured_calls == 0
    store.close()


def test_an_empty_topic_is_the_past_route_and_costs_nothing(tmp_path, theme):
    """`talk to archivist` names nothing, and an NPC greeted with nothing to go
    on volunteers the most important thing it knows. Never spend a tier-1 call
    deciding that nothing means something."""
    store = Store(tmp_path / "w.db")
    client = Recorder()
    router = Router(theme, store, Settings(), client=client)

    assert router.classify("") == ("past", False)
    assert router.classify("   ") == ("past", False)
    assert client.structured_calls == 0
    store.close()


def test_a_miss_escalates_exactly_once(tmp_path, theme):
    store = Store(tmp_path / "w.db")
    client = Recorder(structured_result={"route": "room"})
    router = Router(theme, store, Settings(), client=client)

    assert router.classify("the smell of wet paper") == ("room", True)
    assert client.structured_calls == 1
    store.close()


def test_an_unclassifiable_topic_falls_through_to_the_index(tmp_path, theme):
    """The vector index is the one resolver that can answer an arbitrary topic,
    so a topic no keyword and no classifier could place is exactly what it is
    for. It still ends at "I don't know" when it finds nothing."""
    store = Store(tmp_path / "w.db")
    router = Router(theme, store, Settings(), client=Recorder(structured_result=None))
    assert router.classify("the smell of wet paper")[0] == "past"

    broken = Recorder(structured_result=RuntimeError("model went away"))
    assert Router(theme, store, Settings(), client=broken).classify("qqq")[0] == "past"
    assert Router(theme, store, Settings(), client=None).classify("qqq")[0] == "past"
    store.close()


def test_roster_names_are_routable_without_a_code_change(tmp_path, theme):
    """A new NPC in the theme pack must be askable about, with no edit here."""
    store = Store(tmp_path / "w.db")
    router = Router(theme, store, Settings(), client=Recorder())
    for npc in theme.npcs:
        word = [w for w in npc.name.lower().split() if w not in {"the", "a", "an"}][0]
        assert router.classify(word) == ("npc", False), word
    store.close()


# -- R4: the resolvers -----------------------------------------------------


def test_no_resolver_but_past_touches_the_model(talking):
    """The whole efficiency claim. It would be very easy to have the `npc`
    resolver embed something for ranking; assert on the call count, not intent."""
    engine, state, store, client = talking
    router = engine._router
    actor = actor_of(state)

    for route in ("self", "room", "monsters", "npc", "item", "unknown"):
        client.embedded.clear()
        client.structured_calls = 0
        getattr(router, f"_{route}", lambda *a: [])(state, actor, "anything")
        assert client.embedded == [], f"{route} embedded something"
        assert client.structured_calls == 0, f"{route} made a structured call"


def test_the_past_route_is_the_one_that_embeds(talking):
    engine, state, _, client = talking
    engine._router._past(state, actor_of(state), "the arm")
    assert client.embedded == ["arm"] or client.embedded == ["the arm"]


def test_a_greeting_embeds_the_death_query(talking):
    engine, state, _, client = talking
    engine._router._past(state, actor_of(state), "")
    assert client.embedded == [DEATH_QUERY]


def test_the_monsters_route_sees_one_room_out(talking):
    engine, state, _, _ = talking
    exit_dir, dest = next(iter(state.room.exits.items()))
    state.floor.rooms[dest].actors.append(
        Actor(id="mob:1", name="a duplicate", hp=9, max_hp=9)
    )

    facts = engine._router._monsters(state, actor_of(state), "danger")
    assert any("a duplicate" in f.text and exit_dir.value in f.text for f in facts)


def test_the_monsters_route_ignores_the_dead(talking):
    engine, state, _, _ = talking
    state.room.actors.append(Actor(id="mob:2", name="a partial", hp=0, max_hp=5))
    keys = [f.key for f in engine._router._monsters(state, actor_of(state), "danger")]
    assert "monster:mob:2" not in keys


def test_the_room_route_reads_the_graph_not_the_index(talking):
    engine, state, store, client = talking
    facts = engine._router._room(state, actor_of(state), "here")
    assert any(state.room.name in f.text for f in facts)
    assert any("Ways out" in f.text for f in facts)
    assert client.embedded == []


def test_the_item_route_sees_the_room_and_the_pack(talking):
    engine, state, _, _ = talking
    state.room.items.append(Item(id="i1", name="a copyist's knife"))
    state.player.inventory.append(Item(id="i2", name="a jar of clean water"))

    texts = [f.text for f in engine._router._item(state, actor_of(state), "items")]
    assert any("copyist's knife" in t and "in this room" in t for t in texts)
    assert any("jar of clean water" in t and "carrying" in t for t in texts)


def test_the_npc_route_surfaces_only_who_has_been_met(talking):
    """The Corrector's "Does not trust the Archivist's count" is the material
    this route exists for, and the reason M7 added a second NPC."""
    engine, state, _, _ = talking
    other = next(n for n in engine.theme.npcs if n.anchor != ARCHIVIST)

    assert engine._router._npc(state, actor_of(state), "anyone") == []

    state.met_npcs.add(other.anchor)
    texts = [f.text for f in engine._router._npc(state, actor_of(state), "anyone")]
    assert any(other.name in t for t in texts)
    assert any(other.canon[0] in t for t in texts)


def test_an_npc_is_never_in_its_own_npc_route(talking):
    engine, state, _, _ = talking
    state.met_npcs.add(ARCHIVIST)
    assert engine._router._npc(state, actor_of(state), "anyone") == []


# -- R6: the brief, and "I don't know" -------------------------------------


def test_a_refusal_is_spoken_not_generated(talking):
    """A refusal a 1.7b invents its way around is not a refusal. Measured live:
    told it knew nothing about the Corrector, it explained that the Corrector is
    "a device used to alter records"."""
    engine, state, _, client = talking
    before = client.stream_calls

    said = "".join(e.text for e in engine.turn("ask archivist about anyone else")
                   if isinstance(e, ProseDelta))
    assert said == refusal_of(engine.theme, ARCHIVIST)
    assert client.stream_calls == before, "saying nothing should cost nothing"


def test_the_theme_pack_owns_the_wording_of_a_refusal(tmp_path, theme):
    """Code owns *that* it refuses; the theme owns how it sounds."""
    from simulacra.world.theme import Npc

    store = Store(tmp_path / "w.db")
    router = Router(theme, store, Settings(), client=Recorder())
    first = theme.npcs[0]
    voiced = Npc(anchor=first.anchor, name=first.name, role=first.role,
                 voice=first.voice, depth=first.depth, canon=first.canon,
                 refusal="Not in the ledger.")
    router._theme = type(theme)(name=theme.name, npcs=[voiced])

    assert router._lines(type("A", (), {"id": first.anchor})())[0] == "Not in the ledger."
    store.close()


def test_a_route_that_resolves_to_nothing_is_the_whole_of_t2(talking):
    """No threshold, no calibration against a live embedding model, no guard
    against an outage reading as maximal relevance. An empty list is a truth
    about the data."""
    engine, state, _, _ = talking
    state.room.items.clear()
    state.player.inventory.clear()

    # `item` in a room with nothing in it, and `npc` before anyone has been met:
    # two routes that genuinely resolve to nothing on this floor.
    for topic in ("the blade", "anyone else"):
        brief = engine._router.brief(state, actor_of(state), topic)
        assert brief.facts == [], topic
        assert "do not know anything about that" in brief.instruction, topic


def test_a_greeting_is_answered_by_being_yourself(talking):
    """"I don't know about that" is the wrong answer to someone who walked up
    and said hello -- there was no "that"."""
    engine, state, _, _ = talking
    brief = engine._router.brief(state, actor_of(state), "")
    assert "from what is true of you" in brief.instruction
    assert brief.canon


def test_every_route_carries_an_instruction(talking):
    engine, state, store, client = talking
    state.room.items.append(Item(id="i1", name="a copyist's knife"))
    for topic in ("", "the corridor", "danger", "the blade", "your name",
                  "the last delver", "the corrector", "wet paper"):
        brief = engine._router.brief(state, actor_of(state), topic)
        assert brief.instruction.strip(), f"{topic!r} produced a prompt of pure data"


def test_canon_is_standing_context_wherever_a_route_resolves(talking):
    engine, state, _, _ = talking
    for topic in ("the corridor", "danger"):
        assert engine._router.brief(state, actor_of(state), topic).canon, topic


def test_a_refusal_carries_nothing_to_riff_on(talking):
    """Measured live: an NPC told it knew nothing about the Corrector still had
    its own canon in the prompt and invented that the Corrector is "a tool used
    to adjust entries". On a 1.7b, data beats instruction."""
    engine, state, store, _ = talking
    store.add_canon(ARCHIVIST, "the vault is empty", "told")

    brief = engine._router.brief(state, actor_of(state), "anyone else")
    assert brief.facts == [] and brief.canon == [] and brief.told == []
    assert "do not know anything about that" in brief.instruction


def test_a_repeat_carries_nothing_to_riff_on(talking):
    """Told to say "I already told you" while still holding the material, the
    model said the material again in different words."""
    engine, state, _, _ = talking
    first = engine._router.brief(state, actor_of(state), "the corridor")
    again = engine._router.brief(state, actor_of(state), "the corridor",
                                 surfaced=set(first.keys))
    assert again.repeated
    assert again.facts == [] and again.canon == [] and again.told == []


def test_the_past_route_drops_information_free_chatter(talking):
    """A dialogue transcript records the shape of a question and not its answer.
    M7 found the same thing poisoning derived canon."""
    engine, state, store, _ = talking
    prior = state.run_id - 1
    store.remember(prior, "A delver approached the Archivist and asked about the arm.",
                   kind="dialogue", subjects=[ARCHIVIST], embedding=[0.0] * 768)
    store.remember(prior, "A delver was killed by an offcut on floor 1.",
                   kind="death", subjects=[ARCHIVIST], embedding=[0.0] * 768)
    store.commit()

    texts = [f.text for f in engine._router._past(state, actor_of(state), "the arm")]
    assert any("killed by an offcut" in t for t in texts)
    assert not any("asked about" in t for t in texts)


def test_a_memory_reaches_the_npc_addressed_to_it(talking):
    """M11.1: "The delver approached the Assayer on floor 1..." -- a memory
    written from outside, read back verbatim in the NPC's own mouth."""
    engine, state, store, _ = talking
    store.remember(state.run_id - 1,
                   "A delver left the Archivist on floor 1 and was killed by an offcut.",
                   kind="death", subjects=[ARCHIVIST], embedding=[0.0] * 768)
    store.commit()
    texts = [f.text for f in engine._router._past(state, actor_of(state), "")]
    assert texts == ["A delver left you on floor 1 and was killed by an offcut."]


def test_a_memory_loses_the_bare_name_and_a_leading_one():
    assert Router._to_listener("asked about archivist.", "the Archivist") == "Asked about you."
    assert Router._to_listener("The Archivist watched them go.", "the Archivist") == \
        "You watched them go."


def test_a_greeting_is_not_recorded_as_a_question(talking):
    """"greet assayer" was written down as "asked about assayer" (M11.1), and a
    later run recalled the question nobody asked."""
    engine, _, _, _ = talking
    # No topic: the case that fell back to the addressee.
    summaries = [e.summary for e in engine.turn("talk to archivist")
                 if isinstance(e, Transcript)]
    assert summaries and not any("asked about" in s for s in summaries)


def test_the_self_route_does_not_print_canon_twice(talking):
    engine, state, _, _ = talking
    brief = engine._router.brief(state, actor_of(state), "your name")
    assert brief.canon and brief.facts == []
    assert brief.keys, "self still has to mark what it surfaced"


def test_hearsay_carries_its_attribution_in_the_fact(talking):
    engine, state, store, _ = talking
    store.add_canon(ARCHIVIST, "the vault is empty", "told", confidence=0.5)
    brief = engine._router.brief(state, actor_of(state), "the corridor")
    assert brief.told == ["A delver told you: the vault is empty"]


# -- R5: the conversation session ------------------------------------------


def test_asking_the_same_thing_twice_gets_told_so(talking):
    """And costs nothing. Told to say "I already told you" while holding the
    material, a 1.7b said the material again in different words -- so this
    branch does not generate at all."""
    engine, state, _, client = talking
    list(engine.turn("ask archivist about the corridor"))
    before = client.stream_calls

    said = "".join(e.text for e in engine.turn("ask archivist about the corridor")
                   if isinstance(e, ProseDelta))
    assert said == repeat_of(engine.theme, ARCHIVIST)
    assert client.stream_calls == before, "a repeat should cost no model call"


def test_a_different_route_in_between_is_not_suppressed(talking):
    engine, state, _, client = talking
    state.room.items.append(Item(id="i1", name="a copyist's knife"))

    list(engine.turn("ask archivist about the corridor"))
    list(engine.turn("ask archivist about the blade"))
    assert "already told them this" not in prompt_text(client)


def test_a_new_question_is_never_suppressed(talking):
    """The `past` route collapses many topics onto one small set of memories, so
    facts-alone stonewalled questions the player had never asked. Measured live:
    "ask about the seams in the wall" answered "It is already entered."."""
    engine, state, store, client = talking
    prior = state.run_id - 1
    store.remember(prior, "A delver was killed by an offcut on floor 1.",
                   kind="death", subjects=[ARCHIVIST], embedding=[0.0] * 768)
    store.commit()

    list(engine.turn("talk to archivist"))            # surfaces the death
    said = "".join(e.text for e in engine.turn("ask archivist about the seams")
                   if isinstance(e, ProseDelta))
    assert said != repeat_of(engine.theme, ARCHIVIST)


def test_an_npcs_own_canon_routes_to_itself(talking):
    """The Archivist's canon is all about a ledger, so "the ledger" is a question
    about itself -- but no keyword list written in advance knows that, and the
    classifier sent it elsewhere. Measured live."""
    engine, state, _, client = talking
    before = client.structured_calls  # begin() already ran the director
    assert engine._router.classify("the ledger", actor_of(state)) == ("self", False)
    assert client.structured_calls == before


def test_canon_words_another_route_claims_do_not_hijack_it(talking):
    """The Archivist's canon mentions a floor. Questions about the floor are
    still questions about the room."""
    engine, state, _, _ = talking
    assert engine._router.classify("the floor", actor_of(state))[0] == "room"


def test_a_partly_new_answer_is_not_suppressed(talking):
    """An NPC that stonewalls a question with one new fact in it is worse than
    one that repeats itself."""
    engine, state, store, client = talking
    talk = engine._conversations.setdefault(ARCHIVIST, Conversation(npc_id=ARCHIVIST))
    facts = engine._router._room(state, actor_of(state), "here")
    talk.surfaced.add(facts[0].key)  # one of several

    brief = engine._router.brief(state, actor_of(state), "the corridor",
                                 surfaced=talk.surfaced)
    assert not brief.repeated


def test_nothing_is_surfaced_when_the_guard_swallows_the_reply(talking):
    """A fact marked as said after the NPC said nothing is one the player can
    never hear."""
    engine, state, _, client = talking
    client.script = ["ROOM: the Threshold"]  # trips the echo guard
    list(engine.turn("ask archivist about the corridor"))
    assert engine._conversations[ARCHIVIST].surfaced == set()


def test_the_session_never_reaches_the_store(talking):
    """T3's one load-bearing constraint, carried over unchanged: M4's bug
    happened because a reply reached *persistent* memory."""
    engine, state, store, _ = talking
    list(engine.turn("ask archivist about the corridor"))

    stored = [r["text"] for r in store.db.execute("SELECT text FROM memories")]
    stored += [r["text"] for r in store.canon(ARCHIVIST)]
    assert not any(REPLY in t for t in stored)


def test_the_session_records_what_was_said_in_memory_only(talking):
    engine, _, _, _ = talking
    list(engine.turn("ask archivist about the corridor"))
    talk = engine._conversations[ARCHIVIST]
    assert talk.exchanges and talk.exchanges[-1][1] == REPLY
    assert talk.surfaced


def test_the_session_keeps_only_the_last_two_exchanges(talking):
    engine, _, _, _ = talking
    for i in range(4):
        list(engine.turn(f"ask archivist about the corridor {i}"))
    assert len(engine._conversations[ARCHIVIST].exchanges) <= 2


def test_a_run_that_ends_leaves_no_conversation_state(talking):
    engine, state, _, _ = talking
    list(engine.turn("ask archivist about the corridor"))
    assert engine._conversations

    state.player.hp = 0
    ended = [e for e in engine._die("the dark") if isinstance(e, RunEnded)]
    assert ended
    # An Engine is built once per `new_run` and both frontends break out of
    # their loop on RunEnded, so a run *is* a process -- this is insurance for a
    # future in-process restart, not observable behaviour today.
    engine._conversations.clear()
    assert engine._conversations == {}


# -- the transcript still records the question, never the answer -----------


def test_the_transcript_names_the_route_it_took(talking):
    engine, _, _, _ = talking
    t = next(e for e in engine.turn("ask archivist about the corridor")
             if isinstance(e, Transcript))
    assert "room" in t.tags
    assert REPLY not in t.summary
