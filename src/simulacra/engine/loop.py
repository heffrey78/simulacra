"""The turn loop. A generator of Events -- it never prints, never blocks on a
renderer, and never imports anything from `ui`.

Shape:

    for event in engine.turn(text):
        renderer.handle(event)

Ordering rule: emit cheap, certain events before expensive ones. `RoomEntered`
and `StatusChanged` go out immediately so the UI updates while prose is still
streaming behind them.

MILESTONE M1 (movement) -> M3 (combat) -> M4 (memory).
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass, field

from ..world import discovery
from ..world.director import direct_floor
from ..world.floorgen import floor_rng, generate_floor
from ..world.model import Direction, Item, Room, RoomKind
from .events import (
    Event,
    FloorDescended,
    ItemTaken,
    Line,
    Notice,
    NpcPresent,
    ProseDelta,
    ProseEnd,
    ProseStart,
    Roll,
    RoomEntered,
    RunEnded,
    StatusChanged,
    Thinking,
    Transcript,
)
from .combat import (
    HIDDEN,
    HIDE_DIFFICULTY,
    actors_attack,
    attack_roll,
    best_weapon,
    clear_dead,
    player_attacks,
)
from .judge import adjudicate, apply_verdict
from .parser import _ADDRESS_WORDS, _TOPIC_WORDS, infer, parse
from . import dealings
from .routes import Router
from .state import load_floor_identity, persist_floor, place_npcs

# Read-side caps on canon. The write side caps too (an NPC accumulates canon
# across a world's lifetime), but the prompt is the thing that actually breaks,
# so it enforces its own ceiling rather than trusting every writer.
CANON_LIMIT = 4
TOLD_LIMIT = 2

# How much hearsay one NPC retains, and how much weight it carries. The cap is
# not optional: `told` is a channel the player writes into directly, so an
# uncapped one is both a prompt-bloat vector and a grief vector. Code owns the
# ceiling, as it owns every other budget in this game.
TOLD_CAP = 3
TOLD_CONFIDENCE = 0.5

# M8 moved recall limits, DEATH_QUERY and the topic split out of this module:
# limits and the query live beside the resolver that uses them (`routes.py`),
# and the address/topic split is syntax, so it lives in the parser.

# M1 placeholder prose, one line per structural role. Deliberately flat: this is
# the text the narrator replaces in M2, and it should be obvious that it is a
# placeholder rather than quietly acceptable.
_KIND_BLURB = {
    RoomKind.ENTRANCE: "The way in. Stone underfoot, and the dark ahead.",
    RoomKind.CORRIDOR: "A passage, close and unremarkable.",
    RoomKind.CHAMBER: "A room wide enough to echo.",
    RoomKind.VAULT: "A room built to keep something.",
    RoomKind.LAIR: "Something has been living here.",
    RoomKind.SHRINE: "This room was made with more care than the rest.",
    RoomKind.DESCENT: "Stairs drop away into the dark.",
}


# Words that are never part of who is being addressed. The connective sets come
# from the parser, which is where the address/topic split now happens.
_NAME_NOISE = frozenset({"the", "a", "an"}) | _ADDRESS_WORDS | _TOPIC_WORDS


def _name_words(name: str) -> set[str]:
    return {w for w in name.lower().split() if w not in _NAME_NOISE}


def _addressed(target: str, npcs: list):
    """Which NPC is being spoken to, or None when it is genuinely ambiguous.

    Matches on *word overlap*, not the substring test this used to do. The old
    `needle in a.name.lower()` compared the whole remaining input against the
    name, so `ask archivist about the arm` matched nobody and fell through to
    `npcs[0]` -- correct only while a room could hold at most one NPC, which
    conversation-system.md flagged as fragile and C2 stopped being true.

    Guessing wrong here anchors the wrong NPC's canon and recall, and the
    failure is silent. So an ambiguous address asks.
    """
    words = set(target.lower().split())
    hits = [a for a in npcs if _name_words(a.name) & words]
    if len(hits) == 1:
        return hits[0]
    if not hits and len(npcs) == 1:
        return npcs[0]
    return None


@dataclass
class Conversation:
    """What has already been said to this NPC, this run.

    `surfaced` is why this is not a text window. Tracking *which facts* were
    spoken, by key, means "I've already told you that" is decided in code
    instead of being asked of a 1.7b that has no memory of the previous turn.
    """

    npc_id: str
    exchanges: list[tuple[str, str]] = field(default_factory=list)
    surfaced: set[str] = field(default_factory=set)
    # Topics actually asked, normalised. Suppression needs *both* -- the same
    # question again, and nothing new to say -- because the `past` route
    # collapses many different topics onto one small set of memories, so
    # facts-alone stonewalled questions the player had never asked. Measured
    # live: "ask about the seams in the wall" answered "It is already entered."
    asked: set[str] = field(default_factory=set)


# `enter` descends when aimed at any of these in a descent room.
_STAIR_WORDS = frozenset({
    "stair", "stairs", "steps", "ladder", "ladderway", "shaft", "descent",
    "down", "winze", "chute",
})


def _cap(text: str) -> str:
    """Names carry their article ("the Archivist"); a sentence needs a capital."""
    return text[:1].upper() + text[1:]


class Engine:
    def __init__(
        self,
        state,
        store,
        settings,
        theme,
        *,
        narrator=None,
        prefetcher=None,
        client=None,
    ):
        self.state = state
        self.store = store
        self.settings = settings
        self.theme = theme
        # Absent in M1. The engine must stay runnable with all three as None.
        self.narrator = narrator
        self.prefetcher = prefetcher
        self.client = client
        # Set per turn: did the player actually do something? Only a resolved
        # action provokes the monsters.
        self._resolved = False
        self._last_action = ""

        self._router = Router(theme, store, settings, client=client)
        # Conversation state, per NPC, per run. **Never reaches `Store`**, is
        # never embedded, and dies with the process. That constraint is the
        # whole reason this is safe: M4's self-quotation bug happened because a
        # reply reached *persistent* memory, and a same-run scratch buffer
        # cannot reproduce it.
        self._conversations: dict[str, Conversation] = {}
        # NPCs walking with the player. Run-scoped: their *position* persists on
        # the node, but nobody keeps following you into a new run.
        self._following: set[str] = set()

    # -- public ------------------------------------------------------------

    def begin(self) -> Iterator[Event]:
        """Opening beat: name the floor, describe the entrance, start prefetching."""
        yield Line(self.theme.name, style="title")
        if self.theme.tagline:
            yield Line(self.theme.tagline, style="dim")

        if self.prefetcher is not None:
            self.prefetcher.start()

        # Floor 1 needs an identity like any other floor -- and, like any other
        # floor, only needs it generated once in the life of the world.
        yield from self._establish(self.state.floor)
        yield from self._enter_room(self.state.room_id)

    def turn(self, text: str) -> Iterator[Event]:
        """Parse -> resolve -> narrate -> record. One player input, many events."""
        # Every input costs a turn (M1 behaviour). Whether it costs you a
        # *monster attack* is decided by `_resolved` below.
        self.state.turns += 1
        self._resolved = False

        intent = parse(text)
        if intent is None and self.client is not None:
            # Stage 2: only ever on a stage-1 miss.
            intent = infer(text, self._room_summary(), self.client, self.settings.intent)
        if intent is None:
            yield Notice("I don't understand.")
            return

        self._last_action = intent.raw or text

        match intent.verb:
            case "move":
                yield from self._move(intent.target)
            case "look":
                yield from self._look(intent.target)
            case "take":
                yield from self._take(intent.target)
            case "use":
                yield from self._use(intent.target, intent.raw)
            case "inventory":
                yield from self._inventory()
            case "attack":
                yield from self._attack(intent.target)
            case "descend":
                yield from self.descend()
            case "wait":
                self._resolved = True
                yield Notice("You wait. Nothing obliges you.")
            case "quit":
                yield RunEnded(cause="quit", depth=self.state.depth, turns=self.state.turns)
                return
            case "improvise":
                yield from self._improvise(intent.raw or text)
            case "talk":
                yield from self._talk(intent.addressee, intent.target, intent.raw)
            case "tell":
                yield from self._tell(intent.addressee, intent.target, intent.raw)
            case "give":
                yield from self._give(intent.addressee, intent.target)
            case "request":
                yield from self._request(intent.addressee, intent.target)
            case "follow":
                yield from self._follow(intent.addressee)
            case "search":
                yield from self._search_verb(intent.target, intent.raw)
            case "hide":
                yield from self._hide()
            case "equip":
                yield from self._equip(intent.target)
            case "enter":
                yield from self._enter(intent.target)
            case _:
                yield Notice("I don't understand.")

        # A typo must not get you killed: only a resolved action provokes.
        if self._resolved and self.state.player.alive:
            yield from self._monsters_act()

    def descend(self) -> Iterator[Event]:
        """Generate the next floor.

        M1 does this with no director call and no Thinking event -- floorgen is
        instant. Both arrive in M2, when the descent acquires a real cost.
        """
        if self.state.room.kind is not RoomKind.DESCENT:
            yield Notice("There are no stairs here.")
            return

        self.state.depth += 1
        self.state.player.on_descend(self.state.depth)
        # Never `state.rng`: that is the dice stream, and generating from it
        # made floor N depend on how the player fought on floor N-1.
        floor = generate_floor(
            self.state.depth, self.theme, floor_rng(self.state.world_seed, self.state.depth)
        )

        yield from self._establish(floor)
        place_npcs(self.store, floor, self.theme)

        # Set the floor before entering: _enter_room reads state.floor.
        self.state.floor = floor
        self.state.room_id = floor.entrance_id

        yield FloorDescended(
            depth=self.state.depth,
            theme_name=floor.theme_name or f"floor {self.state.depth}",
            goal=floor.goal,
        )
        yield from self._enter_room(floor.entrance_id)

    # -- verbs -------------------------------------------------------------

    def _move(self, target: str) -> Iterator[Event]:
        if not target:
            yield Notice("Go where?")
            return

        direction = Direction.parse(target)
        if direction is None:
            yield Notice("That isn't a direction.")
            return

        # The ambiguous-`down` resolution from the task doc: the parser stays
        # context-free and returns move(DOWN); deciding it means "descend" needs
        # the current room, so it happens here.
        if direction is Direction.DOWN and self.state.room.kind is RoomKind.DESCENT:
            yield from self.descend()
            return

        dest = self.state.room.exits.get(direction)
        if dest is None and direction is Direction.DOWN:
            # No floor has a down exit except by its stairs (M11.1). The second
            # playtest spent twenty turns pressing `d` in rooms without them;
            # once you have found the stairs, say where they were.
            stairs = next((r for r in self.state.floor.rooms.values()
                           if r.kind is RoomKind.DESCENT and r.visited), None)
            yield Notice("There are no stairs here."
                         + (f" The way down is in {stairs.name}." if stairs else ""))
            return
        if dest is None:
            yield Notice("You can't go that way.")
            return

        # Fleeing is free. An opposed flee roll is a tuning knob to add when
        # there is a reason to, not a default.
        self._resolved = True
        self._bring_followers(self.state.room_id, dest)
        yield from self._enter_room(dest, via=direction.value)

    def _look(self, target: str = "") -> Iterator[Event]:
        if target:
            yield from self._look_at(target)
            return

        room = self.state.room
        yield RoomEntered(
            room_id=room.id, name=room.name,
            exits=self._exits(room), first_visit=False,
        )
        yield from self._describe(room)
        yield from self._contents(room)

    def _look_at(self, target: str) -> Iterator[Event]:
        """Detail on one specific item or actor, generated on demand and
        cached. Free like the whole-room look -- eyeballing something isn't an
        action, so it never sets `_resolved` and never provokes."""
        room = self.state.room
        needle = target.lower()

        item = next((i for i in room.items if needle in i.name.lower()), None)
        if item is not None:
            yield from self._describe_detail("item", item.name)
            return

        actor = next((a for a in room.actors if needle in a.name.lower()), None)
        if actor is not None:
            yield from self._describe_detail("actor", actor.name)
            return

        yield from self._search(target)

    # -- discovery ---------------------------------------------------------

    def _room_canon(self) -> list:
        try:
            return self.store.canon(f"room:{self.state.room_id}", provenance="derived")
        except Exception:
            return []

    def _search(self, target: str) -> Iterator[Event]:
        """The player looked at something the room does not list (M10).

        Before M10 this was always "You don't see that here" -- including for
        nouns the room's own prose had just used.
        """
        room = self.state.room
        wanted = discovery.words(target)

        # Found before? Then it is still there, and still the same thing --
        # looked up by what was searched for, not by overlap with the text.
        index = self.store.node_data(f"room:{self.state.room_id}").get("found") or {}
        for word in wanted:
            if (row := self.store.canon_by_id(index.get(word))) is not None:
                yield ProseStart(channel="detail")
                yield ProseDelta(row["text"])
                yield ProseEnd()
                return

        # The room itself is not a find (M11). Every one of the playtest's ten
        # discoveries was keyed on the room's own name: "search" in the
        # narrowing became a look at "narrowing", which generated a second
        # description of the whole room and filed it as something found.
        if wanted and wanted <= discovery.words(room.name):
            # Through `_look`, not `_describe`: room text has exactly one
            # producer, and test_seam enforces who may call it. Searching the
            # room you are standing in is looking around, which is what `_look`
            # already is.
            yield from self._look()
            return

        if self.narrator is None:
            yield Notice(f"You don't see {target} here.")
            return

        if len(self._room_canon()) >= discovery.DISCOVERY_BUDGET:
            # Exhausted. No roll, no call -- the budget caps new content, not
            # access to what is already there.
            yield Notice(f"You don't see {target} here.")
            return

        if discovery.grade(target, room, self.theme,
                           world_seed=self.state.world_seed) == "absent":
            yield Notice(f"You don't see {target} here.")
            return

        yield from self._discover(target)

    def _discover(self, target: str) -> Iterator[Event]:
        """Describe one find and write it down. The caller has decided it may.

        Rummaging is not eyeballing: a plain `look` stays free and never
        provokes; turning something up takes a turn, and doing it with
        something hostile in the room should cost you.
        """
        self._resolved = True
        wanted = discovery.words(target)

        yield ProseStart(channel="detail")
        parts: list[str] = []
        for piece in self.narrator.discover(target, self.state.room):
            parts.append(piece)
            yield ProseDelta(piece)
        yield ProseEnd()

        text = "".join(parts).strip()
        if not text or text == self.narrator.NOTHING_FOUND:
            # A rejected generation must not become permanent canon: canon
            # outlives the run, and M7 paid a live five-run read to learn it.
            return
        try:
            node = f"room:{self.state.room_id}"
            canon_id = self.store.add_canon(node, text, "derived",
                                            source_run=self.state.run_id)
            # Every word of what was searched for points at this find, so "the
            # cracked altar" and "altar" both reach it later.
            data = self.store.node_data(node)
            data["found"] = {**(data.get("found") or {}),
                             **{w: canon_id for w in wanted}}
            self.store.set_node_data(node, data)
        except Exception:
            pass

    def _search_room(self) -> Iterator[Event]:
        """Bare `search`: look for what the room has *not* already said (M11).

        The playtest's complaint was that search re-described the room. So this
        only ever reaches for a fixture the room's name, concept and prose have
        not used, and only in a fertile room with budget left. Otherwise there is
        nothing more to find, and saying so costs no model call.
        """
        room = self.state.room
        self._resolved = True
        nothing = Notice("You find nothing more here.")

        if self.narrator is None:
            yield nothing
            return
        found = self._room_canon()
        if (len(found) >= discovery.DISCOVERY_BUDGET
                or not discovery.is_fertile(room.id, self.state.world_seed)):
            yield nothing
            return

        index = self.store.node_data(f"room:{room.id}").get("found") or {}
        said = (discovery.words(room.name) | discovery.words(room.concept)
                | discovery.words(room.prose))
        fresh = [
            f for f in self.theme.fixture_names(room.kind)
            if not discovery.words(f) & (said | set(index))
        ]
        if not fresh:
            yield nothing
            return

        # Seeded by room and by how much it has already given up, so the same
        # room yields the same things in the same order in every run.
        pick = random.Random(
            f"search:{self.state.world_seed}:{room.id}:{len(found)}"
        ).choice(fresh)
        yield from self._discover(pick)

    def _search_verb(self, target: str, raw: str = "") -> Iterator[Event]:
        """`search`, `rummage`, `loot`."""
        if raw.split()[:1] == ["loot"] and target:
            room = self.state.room
            wanted = discovery.words(target)
            here = [*room.items, *room.actors]
            if not any(wanted & discovery.words(x.name) for x in here):
                # The dead are removed when they fall, and carry nothing. Say so
                # rather than search the walls, which is what this used to do.
                yield Notice("Whatever that was, it left nothing behind to take.")
                return
        if target:
            yield from self._look_at(target)
            return
        yield from self._search_room()

    def _hide(self) -> Iterator[Event]:
        """Get out of sight (M11). A roll, a turn, and a real effect.

        Hidden, the next monster round passes you by, and your next attack gets
        a bonus and ends it. You cannot hide again while you are still hidden.
        """
        player = self.state.player
        if HIDDEN in player.effects:
            yield Notice("You're already out of sight.")
            return
        hostiles = [a for a in self.state.room.actors if a.hostile and a.hp > 0]
        if not hostiles:
            yield Notice("There's nothing here to hide from.")
            return

        self._resolved = True
        total, success = attack_roll(player.attack, HIDE_DIFFICULTY, self.state.rng)
        yield Roll(label="hide", total=total, target=HIDE_DIFFICULTY, success=success)
        if not success:
            return
        # Two: it has to survive the tick that follows this turn's monster round
        # to still be there for the attack that ends it.
        player.effects[HIDDEN] = 2
        yield Line("You get out of sight.", style="good")
        yield self._status()

    def _equip(self, target: str) -> Iterator[Event]:
        """Say what is in hand -- honestly (M11).

        Combat always swings the highest-damage weapon carried. Until weapons
        differ in more than damage, letting the player pick a worse one would be
        a choice with no decision in it, so `equip` reports rather than changes.
        """
        weapon = best_weapon(self.state.player)
        if target:
            wanted = discovery.words(target)
            named = next((i for i in self.state.player.inventory
                          if wanted & discovery.words(i.name)), None)
            if named is None:
                yield Notice("You aren't carrying that.")
                return
            if named.damage <= 0:
                yield Line(f"{_cap(named.name)} is no weapon.")
                return
            if weapon is not None and named is not weapon:
                yield Line(f"You keep {weapon.name} in hand; it hits harder than {named.name}.")
                return
        if weapon is None:
            yield Line("You have nothing to fight with but your hands.")
            return
        yield Line(f"You're holding {weapon.name}. You always fight with the best you carry.")

    def _enter(self, target: str) -> Iterator[Event]:
        """`enter <place>`: the stairs, a neighbouring room by name, or a look (M11)."""
        room = self.state.room
        wanted = discovery.words(target)

        if room.kind is RoomKind.DESCENT and (
            not wanted or wanted & _STAIR_WORDS or wanted <= discovery.words(room.name)
        ):
            yield from self.descend()
            return
        if not wanted:
            yield Notice("Enter what?")
            return

        for direction, dest in room.exits.items():
            neighbour = self.state.floor.rooms.get(dest)
            if neighbour is not None and wanted & discovery.words(neighbour.name):
                yield from self._move(direction.value)
                return

        yield from self._look_at(target)

    def _describe_detail(self, kind: str, name: str) -> Iterator[Event]:
        if self.narrator is None:
            yield Line(f"Nothing more to notice about {name}.")
            return

        yield ProseStart(channel="detail")
        for piece in self.narrator.detail(kind, name, self.state.room.concept):
            yield ProseDelta(piece)
        yield ProseEnd()

    def _take(self, target: str) -> Iterator[Event]:
        if not target or not target.strip():
            yield Notice("Take what?")
            return

        room = self.state.room
        needle = target.lower()
        # Substring match: players type "take water", not the full item name.
        for item in room.items:
            if needle in item.name.lower():
                room.items.remove(item)
                self.state.player.inventory.append(item)
                self._resolved = True
                yield ItemTaken(item=item.name)
                return

        # Something the room's own text describes, but not something to carry
        # (M12). "A rusted iron key lies in the wall" followed by "There is no
        # key here" was the engine contradicting its own prose, three times in
        # the second playtest.
        if discovery.grade(target, room, self.theme,
                           world_seed=self.state.world_seed) == "mentioned":
            yield Notice(f"The {target} is part of the room, not something you can carry.")
            return
        yield Notice(f"There is no {target} here.")

    def _use(self, target: str, raw: str = "") -> Iterator[Event]:
        player = self.state.player
        needle = (target or "").strip().lower()
        usable = [i for i in player.inventory if i.heal > 0]
        item = next((i for i in player.inventory
                     if needle and needle in i.name.lower()), None)

        if item is None:
            # One thing you could mean, and you asked to drink or eat, or named
            # nothing: use it (M11.1). The second playtest carried a canteen and
            # got "Use what?" for `drink`, then "You aren't carrying water" for
            # `drink water` -- Hardpan's drinks are not called water.
            consuming = raw.split()[:1] in (["drink"], ["eat"], ["consume"], ["quaff"])
            if len(usable) == 1 and (not needle or consuming):
                item = usable[0]
            elif not needle:
                names = ", ".join(i.name for i in usable)
                yield Notice("Use what?" + (f" You have {names}." if usable else ""))
                return
            else:
                yield Notice(f"You aren't carrying {target}.")
                return

        if item.heal <= 0:
            yield Notice(f"You can't think what to do with {item.name}.")
            return
        healed = min(item.heal, player.max_hp - player.hp)
        player.hp += healed
        player.inventory.remove(item)
        self._resolved = True
        yield Line(f"You use {item.name}. ({healed:+d} hp)", style="good")
        yield self._status()

    def _attack(self, target: str) -> Iterator[Event]:
        room = self.state.room

        # M11: the wary band's door. Attacking an NPC was filtered out before it
        # reached them, so disposition could never go below zero by anything a
        # player could do. NPCs are persistent and cannot be hurt -- but they can
        # be frightened, and they remember it. Only when named: a bare `attack`
        # never starts a grudge by accident.
        wanted = discovery.words(target)
        npc = next((a for a in room.actors
                    if not a.hostile and wanted & discovery.words(a.name)), None)
        if npc is not None:
            self._resolved = True
            dealings.bump(self.store, npc.id, dealings.ATTACK_STEP)
            self._following.discard(npc.id)
            yield from self._says(npc, "They step back from you, and do not come closer.")
            yield Line(f"{_cap(npc.name)} will remember that.", style="alert")
            return

        hostiles = [a for a in room.actors if a.hostile and a.hp > 0]
        if not hostiles:
            yield Notice("There is nothing here to fight.")
            return

        needle = target.lower()
        victim = next((a for a in hostiles if needle and needle in a.name.lower()),
                      hostiles[0])

        self._resolved = True
        yield from player_attacks(self.state.player, victim, self.state.rng)
        for dead in clear_dead(room.actors):
            yield Line(f"{dead.name} is finished.", style="good")

    def _improvise(self, action: str) -> Iterator[Event]:
        if self.client is None:
            yield Notice("I don't understand.")
            return

        verdict = adjudicate(
            action, self._room_summary(), self.theme, self.client, self.settings.judge
        )
        self._resolved = True
        self.state._last_action = action
        yield from apply_verdict(verdict, self.state, self.state.rng)
        for dead in clear_dead(self.state.room.actors):
            yield Line(f"{dead.name} is finished.", style="good")

    # -- talking -----------------------------------------------------------

    def _talk(self, addressee: str, topic: str, line: str) -> Iterator[Event]:
        """One dialogue turn: who, then what about, then what they are told.

        The retrieval decision belongs to `routes.Router` and the phrasing to
        the narrator; this only resolves *which* NPC and keeps the session.
        """
        npcs = [a for a in self.state.room.actors if not a.hostile]
        if not npcs:
            yield Notice("There is no one here to talk to.")
            return
        if self.narrator is None:
            yield Notice("They have nothing to say.")
            return

        actor = _addressed(addressee or topic, npcs)
        if actor is None:
            yield Notice("Which of them? " + ", ".join(a.name for a in npcs) + ".")
            return

        persona = self._personas().get(actor.id)
        if persona is None:
            yield Notice("They have nothing to say.")
            return

        talk = self._conversations.setdefault(actor.id, Conversation(npc_id=actor.id))
        key = topic.strip().lower()
        brief = self._router.brief(
            self.state, actor, topic,
            # A repeat is a repeated *question* that has nothing new behind it.
            # The same fact in answer to a new question is fine; the player
            # asked something else and deserves an answer.
            surfaced=talk.surfaced if key in talk.asked else None,
        )
        talk.asked.add(key)
        # Only what came from a previous *run*: room entry reads this for the
        # "(remembers you)" tag. M8 filled it from any route's answer, so the
        # second playtest's Assayer "remembered" the delver after one question
        # about a tin of peaches.
        actor.recollections = [f.text for f in brief.facts if f.key.startswith("memory:")]

        # Talking is a resolved action, so anything hostile in the room gets its
        # turn. Conversation in a monster's presence is a choice with a price.
        self._resolved = True

        # "(remembers you)" means a prior *run* surfaced, which is only ever the
        # `past` route. Authored canon is what the NPC is, not what it recalls
        # about this delver, and counting it would light the tag on turn one of
        # a brand new world.
        yield NpcPresent(
            npc_id=actor.id, name=actor.name,
            remembers=any(f.key.startswith("memory:") for f in brief.facts),
        )
        yield ProseStart(channel="npc", speaker=actor.name)
        spoken: list[str] = []
        if brief.spoken:
            # No model call: a refusal a 1.7b invents its way around is not a
            # refusal. See routes.DEFAULT_REFUSAL.
            spoken.append(brief.spoken)
            yield ProseDelta(brief.spoken)
        else:
            for piece in self.narrator.npc(persona, line, brief):
                spoken.append(piece)
                yield ProseDelta(piece)
        yield ProseEnd()

        said = "".join(spoken).strip()
        if said and said != "They say nothing.":
            # Only once the reply actually happened. The echo and degeneracy
            # guards can swallow a whole generation, and a fact marked as said
            # after the NPC said nothing is one the player can never hear.
            talk.surfaced.update(brief.keys)
            talk.exchanges.append((topic, said))
            del talk.exchanges[:-2]

        self.store.upsert_node(actor.id, "npc", actor.name, run_id=self.state.run_id)
        self.state.met_npcs.add(actor.id)

        # The conversation becomes recallable -- but records only what the
        # *delver* brought, never the NPC's own reply. Storing the reply feeds
        # an NPC its own words on the next run, and the loop compounds: three
        # runs in, recall was two self-quotations crowding out an actual death.
        asked = (topic or addressee or self._last_action).strip()
        yield Transcript(
            summary=(f"A delver approached {actor.name} on floor {self.state.depth}"
                     + (f" and asked about {asked}." if asked else ".")),
            kind="dialogue",
            subjects=(actor.id, f"room:{self.state.room_id}"),
            tags=("dialogue", brief.route),
        )

    def _tell(self, addressee: str, claim: str, line: str) -> Iterator[Event]:
        """The player asserts something. It becomes hearsay, and may be false.

        This is the narrow, safe half of "NPCs learn from what players say" --
        the half M4's bug made everyone afraid of. It is safe because it is
        *typed*: the row is `provenance='told'`, it is never promoted to
        `derived` or `observed` by any code path, and the prompt labels it as
        something a delver claimed. A lying player produces an NPC that believes
        something false, which is content.
        """
        npcs = [a for a in self.state.room.actors if not a.hostile]
        if not npcs:
            yield Notice("There is no one here to tell.")
            return

        actor = _addressed(addressee or claim, npcs)
        if actor is None:
            yield Notice("Tell which of them? " + ", ".join(a.name for a in npcs) + ".")
            return

        claim = claim.strip()
        if not claim:
            yield Notice(f"Tell {actor.name} what?")
            return

        try:
            self.store.add_canon(
                actor.id, claim, "told",
                source_run=self.state.run_id, confidence=TOLD_CONFIDENCE,
            )
            self.store.retire_canon(actor.id, provenance="told", keep_newest=TOLD_CAP)
        except Exception:
            # A world that cannot record the claim can still hear it.
            pass

        # Then they answer, with the claim already in their prompt.
        yield from self._talk(addressee, claim, line)

    def _personas(self) -> dict:
        return {n.anchor: n for n in self.theme.npcs}

    # -- dealings ----------------------------------------------------------

    def _face(self, addressee: str, verb: str):
        """Resolve who is being dealt with, or emit the refusal and return None."""
        npcs = [a for a in self.state.room.actors if not a.hostile]
        if not npcs:
            return None, Notice(f"There is no one here to {verb}.")
        actor = _addressed(addressee, npcs)
        if actor is None:
            return None, Notice("Which of them? " + ", ".join(a.name for a in npcs) + ".")
        return actor, None

    def _says(self, actor, text: str) -> Iterator[Event]:
        """An NPC speaks a line we already have. No model call."""
        yield ProseStart(channel="npc", speaker=actor.name)
        yield ProseDelta(text)
        yield ProseEnd()

    def _give(self, addressee: str, item_name: str) -> Iterator[Event]:
        """Hand something over. The player's pack is checked in code first --
        whether they are carrying it is not a question for the model."""
        actor, problem = self._face(addressee, "give to")
        if problem is not None:
            yield problem
            return

        needle = (item_name or "").strip().lower()
        item = next(
            (i for i in self.state.player.inventory
             if needle and (needle in i.name.lower() or i.name.lower() in needle)),
            None,
        )
        if item is None:
            yield Notice("You are not carrying that.")
            return

        persona = self._personas().get(actor.id)
        if persona is None:
            yield Notice("They want nothing from you.")
            return

        self._resolved = True
        if dealings.is_wary(self.store, actor.id):
            # Free: M8's rule is that a refusal code can decide is code's job.
            yield from self._says(actor, "They will not take it from you.")
            return

        decision = dealings.decide_gift(persona, item, self.client, self.settings.judge)
        if decision.act != "accept":
            line = decision.reason if dealings.presentable(decision.reason) else ""
            yield from self._says(actor, line or "They refuse it.")
            return

        self.state.player.inventory.remove(item)
        dealings.take(self.store, actor.id, item)
        dealings.bump(self.store, actor.id, dealings.GIFT_STEP)
        self.state.met_npcs.add(actor.id)

        line = decision.reason if dealings.presentable(decision.reason) else ""
        # Names carry their article ("the Archivist"), so a fallback that starts
        # with one needs the capital putting back.
        fallback = f"{actor.name} takes it."
        yield from self._says(actor, line or fallback[0].upper() + fallback[1:])
        yield Line(f"You give {item.name} to {actor.name}.", style="good")
        yield Transcript(
            summary=f"A delver gave {item.name} to {actor.name} on floor {self.state.depth}.",
            kind="event", subjects=(actor.id, f"room:{self.state.room_id}"),
            tags=("gift",),
        )

    def _request(self, addressee: str, item_name: str) -> Iterator[Event]:
        """Ask for something they carry. Disposition answers before the model does."""
        actor, problem = self._face(addressee, "ask")
        if problem is not None:
            yield problem
            return

        persona = self._personas().get(actor.id)
        if persona is None:
            yield Notice("They have nothing for you.")
            return

        self._resolved = True
        rows = dealings.holdings(self.store, actor.id)
        if dealings.is_wary(self.store, actor.id) or not rows:
            yield from self._says(actor, "They have nothing for you.")
            return

        decision = dealings.decide_request(
            persona, item_name, rows, self.client, self.settings.judge
        )
        row = dealings.match(decision.object, rows) if decision.act == "give" else None
        if row is None:
            line = decision.reason if dealings.presentable(decision.reason) else ""
            yield from self._says(actor, line or "They keep what they have.")
            return

        taken = dealings.release(self.store, actor.id, row["id"])
        if taken is None:  # lost a race with itself; refuse rather than duplicate
            yield from self._says(actor, "They keep what they have.")
            return

        self.state.player.inventory.append(Item(
            id=taken["id"], name=taken["name"],
            heal=int(taken.get("heal") or 0), damage=int(taken.get("damage") or 0),
        ))
        line = decision.reason if dealings.presentable(decision.reason) else ""
        yield from self._says(actor, line or f"They hand you {taken['name']}.")
        yield Line(f"You take {taken['name']}.", style="good")

    def _follow(self, addressee: str) -> Iterator[Event]:
        """Walk with me. Disposition answers -- no model call, ever.

        Asking a 1.7b whether it feels like walking with you spends a tier-1
        call on a decision code makes correctly every time.
        """
        actor, problem = self._face(addressee, "ask")
        if problem is not None:
            yield problem
            return

        self._resolved = True
        if actor.id in self._following:
            self._following.discard(actor.id)
            yield from self._says(actor, "They stop where they are.")
            return

        if not dealings.is_warm(self.store, actor.id):
            yield from self._says(actor, "They stay where they are.")
            return

        self._following.add(actor.id)
        yield from self._says(actor, "They fall in behind you.")

    def _bring_followers(self, from_id: str, to_id: str) -> None:
        """Move anyone walking with the player, and write down where they are.

        M7 inverted placement so `floorgen` proposes and the world disposes.
        This is the task that cashes that claim, and it is these nine lines.
        """
        if not self._following:
            return
        src, dst = self.state.floor.rooms.get(from_id), self.state.floor.rooms.get(to_id)
        if src is None or dst is None:
            return
        for anchor in list(self._following):
            actor = next((a for a in src.actors if a.id == anchor), None)
            if actor is None:
                continue
            src.actors.remove(actor)
            dst.actors.append(actor)
            data = self.store.node_data(anchor)
            data["room_id"] = to_id
            self.store.set_node_data(anchor, data)

    # -- consequence -------------------------------------------------------

    def _monsters_act(self) -> Iterator[Event]:
        room = self.state.room
        hostiles = [a for a in room.actors if a.hostile and a.hp > 0]
        if hostiles:
            if HIDDEN in self.state.player.effects:
                yield Line("They don't find you.", style="dim")
            else:
                yield from actors_attack(hostiles, self.state.player, self.state.rng)

        yield from self._tick_effects()

        if not self.state.player.alive:
            killer = hostiles[0].name if hostiles else "the dark"
            yield from self._die(killer)

    def _tick_effects(self) -> Iterator[Event]:
        """Count effects down, and report only when one *ends*.

        It used to report every tick, so a status applied this turn printed
        twice -- once when applied, once when ticked. The playtest's "[braced]"
        line appeared twice for exactly that reason.
        """
        effects = self.state.player.effects
        if not effects:
            return
        expired = False
        for name in list(effects):
            effects[name] -= 1
            if effects[name] <= 0:
                del effects[name]
                expired = True
        if expired:
            yield self._status()

    def _die(self, cause: str) -> Iterator[Event]:
        epitaph = ""
        if self.narrator is not None:
            try:
                epitaph = self.narrator.epitaph(cause, self.state.depth, self.state.turns)
            except Exception:
                epitaph = ""

        self._record_death(cause)
        yield Transcript(
            summary=(f"A delver was killed by {cause} on floor {self.state.depth}, "
                     f"after {self.state.turns} turns."),
            kind="death",
            subjects=(*sorted(self.state.met_npcs), f"room:{self.state.room_id}"),
            tags=("death", cause),
        )
        yield RunEnded(
            cause=cause, depth=self.state.depth,
            turns=self.state.turns, epitaph=epitaph,
        )

    def _record_death(self, cause: str) -> None:
        """Graph only -- embeddings are M4. Written now for the same reason M1
        persisted floors: ten lines here, a painful backfill later, and by M4 the
        interesting deaths will already have happened."""
        run = f"run:{self.state.run_id}"
        self.store.upsert_node(run, "run", f"run {self.state.run_id}",
                               run_id=self.state.run_id)
        self.store.link(run, "DIED_IN", f"room:{self.state.room_id}",
                        run_id=self.state.run_id)
        killer = f"actor:{cause}"
        self.store.upsert_node(killer, "actor", cause, run_id=self.state.run_id)
        self.store.link(run, "KILLED_BY", killer, run_id=self.state.run_id)
        self.store.commit()

    def _room_summary(self) -> str:
        """Compact context for the tier-1 calls. Kept small: prompt eval is not
        free on CPU either."""
        room = self.state.room
        parts = [room.name]
        if room.items:
            parts.append("items: " + ", ".join(i.name for i in room.items))
        if room.actors:
            parts.append("present: " + ", ".join(a.name for a in room.actors))
        if not any(a.hostile and a.hp > 0 for a in room.actors):
            # Said outright (M11). Left unsaid, the judge invented an enemy for
            # "jump" and "hide" in an empty room and ruled on it.
            parts.append("nothing hostile is here")
        parts.append("exits: " + ", ".join(d.value for d in room.exits))
        return ". ".join(parts)

    def _inventory(self) -> Iterator[Event]:
        inv = self.state.player.inventory
        if not inv:
            yield Line("You carry nothing.", style="dim")
            return
        yield Line("You carry:", style="dim")
        for item in inv:
            yield Line(f"  {item.name}")

    # -- shared ------------------------------------------------------------

    def _establish(self, floor) -> Iterator[Event]:
        """Give a floor its identity, then write it down.

        A floor this world has already named is re-attached for free. That skip
        is the point of persisting floors: the ~19 s director call is paid once
        per floor for the life of the world, not once per floor per run -- so
        the second run through a world waits at no descent at all.

        Persist happens here, after the identity exists either way. Persisting
        before directing is what left `floor:1` named "floor 1" forever.
        """
        if load_floor_identity(self.store, floor):
            if floor.theme_name:
                self.state.floor_history.append(floor.theme_name)
        else:
            yield from self._direct(floor)
        persist_floor(self.store, floor, self.state.run_id)

    def _direct(self, floor) -> Iterator[Event]:
        """Tier 3. The one place in the loop where the player waits on a spinner."""
        if self.client is None:
            return
        yield Thinking("The floor takes shape")
        others = self.store.floor_identities(except_depth=floor.depth)
        ok = direct_floor(
            floor, self.theme, self.client, self.settings.director,
            # From the world, not from this run's traversal: run 2's first
            # descent has walked nothing, and would otherwise tell the director
            # to avoid nothing while the world already uses those names.
            previously=self.store.floor_names(below_depth=floor.depth),
            # M12: every other floor's moods and names, which code enforces --
            # the model was told the names before and repeated them anyway.
            avoid_motifs=[m for d in others for m in (d.get("motifs") or [])],
            used_names=[d["theme_name"] for d in others if d.get("theme_name")],
        )
        if ok and floor.theme_name:
            self.state.floor_history.append(floor.theme_name)

    def _enter_room(self, room_id: str, *, via: str | None = None) -> Iterator[Event]:
        self.state.room_id = room_id
        room = self.state.room

        first_visit = not room.visited
        room.visited = True

        # Cheap and certain first, so a future TUI can repaint its panels while
        # prose is still arriving behind them.
        yield RoomEntered(
            room_id=room.id, name=room.name,
            exits=self._exits(room), first_visit=first_visit, via=via,
        )
        yield self._status()

        if self.prefetcher is not None:
            # Only take the model back if we actually need it. Preempting on a
            # cache hit would kill useful background work for nothing.
            if not self.prefetcher.has_prose(self.state.floor, room):
                self.prefetcher.preempt()

        yield from self._describe(room)
        yield from self._contents(room)

        if self.prefetcher is not None:
            # Only the neighbours are reachable next turn; nothing else is worth
            # spending the model on. This also clears the preempt flag.
            self.prefetcher.schedule(self.state.floor, list(room.exits.values()))

    def _describe(self, room: Room) -> Iterator[Event]:
        """The prose slot -- and the ONLY place room text is produced.

        M1 yielded a flat `Line`; M2 streams from the narrator. The fallback is
        not dead code -- it is what keeps the engine runnable with no model, and
        the whole M1 test suite still exercises it.
        """
        if self.narrator is None:
            room.prose = room.concept or _KIND_BLURB[room.kind]
            yield Line(room.prose)
            return

        yield ProseStart(channel="room")
        parts: list[str] = []
        for piece in self.narrator.room(self.state.floor, room):
            parts.append(piece)
            yield ProseDelta(piece)
        yield ProseEnd()

        # `Room.prose` and `Room.described` were declared in M1 and nothing ever
        # wrote to them -- the text lived only in `prose_cache`, keyed by a hash.
        # M10 needs it: the player reads the *prose*, so the nouns they can look
        # at have to come from the prose and not only from the director's
        # one-line concept.
        room.prose = "".join(parts).strip()

    def _contents(self, room: Room) -> Iterator[Event]:
        """Items and actors. Separate from prose: in M2 the narrator may mention
        these in passing, but the player still needs an unambiguous list."""
        for actor in room.actors:
            if actor.hostile:
                yield Line(f"{actor.name} is here.", style="alert")
            else:
                yield NpcPresent(npc_id=actor.id, name=actor.name,
                                 remembers=bool(actor.recollections))
        for item in room.items:
            yield Line(f"You see {item.name}.", style="good")

    def _status(self) -> StatusChanged:
        p = self.state.player
        return StatusChanged(
            hp=p.hp, max_hp=p.max_hp, depth=self.state.depth,
            effects=tuple(p.effects),
        )

    @staticmethod
    def _exits(room: Room) -> tuple[str, ...]:
        return tuple(d.value for d in room.exits)
