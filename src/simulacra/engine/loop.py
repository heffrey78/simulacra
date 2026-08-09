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

from collections.abc import Iterator

from ..world.director import direct_floor
from ..world.floorgen import generate_floor
from ..world.model import Direction, Room, RoomKind
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
    RoomEntered,
    RunEnded,
    StatusChanged,
    Thinking,
    Transcript,
)
from .combat import actors_attack, clear_dead, player_attacks
from .judge import adjudicate, apply_verdict
from .parser import infer, parse
from .state import persist_floor

# Two or three recollections, never more. Prompt bloat is real at 4096 context,
# and a small model handed six memories recites a list instead of speaking.
RECALL_LIMIT = 3

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

    # -- public ------------------------------------------------------------

    def begin(self) -> Iterator[Event]:
        """Opening beat: name the floor, describe the entrance, start prefetching."""
        yield Line(self.theme.name, style="title")
        if self.theme.tagline:
            yield Line(self.theme.tagline, style="dim")

        if self.prefetcher is not None:
            self.prefetcher.start()

        # Floor 1 needs a director pass like any other floor.
        yield from self._direct(self.state.floor)
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
                yield from self._look()
            case "take":
                yield from self._take(intent.target)
            case "use":
                yield from self._use(intent.target)
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
                yield from self._talk(intent.target)
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
        floor = generate_floor(self.state.depth, self.theme, self.state.rng)

        # Director before persist, so the floor node carries its real name.
        yield from self._direct(floor)
        persist_floor(self.store, floor, self.state.run_id)

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
        if dest is None:
            yield Notice("You can't go that way.")
            return

        # Fleeing is free. An opposed flee roll is a tuning knob to add when
        # there is a reason to, not a default.
        self._resolved = True
        yield from self._enter_room(dest, via=direction.value)

    def _look(self) -> Iterator[Event]:
        room = self.state.room
        yield RoomEntered(
            room_id=room.id, name=room.name,
            exits=self._exits(room), first_visit=False,
        )
        yield from self._describe(room)
        yield from self._contents(room)

    def _take(self, target: str) -> Iterator[Event]:
        if not target:
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

        yield Notice(f"There is no {target} here.")

    def _use(self, target: str) -> Iterator[Event]:
        player = self.state.player
        if not target:
            yield Notice("Use what?")
            return

        needle = target.lower()
        for item in player.inventory:
            if needle not in item.name.lower():
                continue
            if item.heal <= 0:
                yield Notice(f"You can't think what to do with {item.name}.")
                return
            healed = min(item.heal, player.max_hp - player.hp)
            player.hp += healed
            player.inventory.remove(item)
            self._resolved = True
            yield Line(f"You use {item.name}. ({healed:+d} hp)", style="good")
            yield self._status()
            return

        yield Notice(f"You aren't carrying {target}.")

    def _attack(self, target: str) -> Iterator[Event]:
        room = self.state.room
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

    def _talk(self, target: str) -> Iterator[Event]:
        room = self.state.room
        npcs = [a for a in room.actors if not a.hostile]
        if not npcs:
            yield Notice("There is no one here to talk to.")
            return
        if self.narrator is None:
            yield Notice("They have nothing to say.")
            return

        needle = target.lower()
        actor = next((a for a in npcs if needle and needle in a.name.lower()), npcs[0])
        persona = self._personas().get(actor.id)
        if persona is None:
            yield Notice("They have nothing to say.")
            return

        recollections = self._recall_for(actor.id)
        actor.recollections = [r.text for r in recollections]

        # Talking is a resolved action, so anything hostile in the room gets its
        # turn. Conversation in a monster's presence is a choice with a price.
        self._resolved = True

        yield NpcPresent(npc_id=actor.id, name=actor.name,
                         remembers=bool(recollections))
        yield ProseStart(channel="npc", speaker=actor.name)
        spoken: list[str] = []
        for piece in self.narrator.npc(persona, target, recollections):
            spoken.append(piece)
            yield ProseDelta(piece)
        yield ProseEnd()

        self.store.upsert_node(actor.id, "npc", actor.name, run_id=self.state.run_id)
        self.state.met_npcs.add(actor.id)

        # The conversation becomes recallable -- but records only what the
        # *delver* brought, never the NPC's own reply. Storing the reply feeds
        # an NPC its own words on the next run, and the loop compounds: three
        # runs in, recall was two self-quotations crowding out an actual death.
        asked = (target or self._last_action).strip()
        yield Transcript(
            summary=(f"A delver approached {actor.name} on floor {self.state.depth}"
                     + (f" and asked about {asked}." if asked else ".")),
            kind="dialogue",
            subjects=(actor.id, f"room:{self.state.room_id}"),
            tags=("dialogue",),
        )

    def _personas(self) -> dict:
        return {n.anchor: n for n in self.theme.npcs}

    def _recall_for(self, anchor: str) -> list:
        """Graph-anchored, then semantically ranked. Prior runs only.

        `exclude_run` is not optional: without it an NPC 'remembers' something
        from four turns ago as though it were a past life.
        """
        if self.client is None:
            return []
        # Phrased about outcomes, not conversations. "Who spoke with X" ranked
        # the NPC's own past conversations above an actual death, because those
        # memories are literally about speaking.
        query = "How did the previous delver die, and on which floor?"
        try:
            embedding = self.client.embed([query], self.settings.embed)[0]
        except Exception:
            embedding = None
        try:
            found = self.store.recall(
                embedding=embedding, about=anchor,
                exclude_run=self.state.run_id, limit=RECALL_LIMIT,
            )
        except Exception:
            return []

        # Deaths last: a small model attends hardest to the end of its prompt,
        # and a death is the most worth saying out loud.
        return sorted(found, key=lambda r: r.kind == "death")

    # -- consequence -------------------------------------------------------

    def _monsters_act(self) -> Iterator[Event]:
        room = self.state.room
        hostiles = [a for a in room.actors if a.hostile and a.hp > 0]
        if hostiles:
            yield from actors_attack(hostiles, self.state.player, self.state.rng)

        yield from self._tick_effects()

        if not self.state.player.alive:
            killer = hostiles[0].name if hostiles else "the dark"
            yield from self._die(killer)

    def _tick_effects(self) -> Iterator[Event]:
        effects = self.state.player.effects
        if not effects:
            return
        for name in list(effects):
            effects[name] -= 1
            if effects[name] <= 0:
                del effects[name]
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

    def _direct(self, floor) -> Iterator[Event]:
        """Tier 3. The one place in the loop where the player waits on a spinner."""
        if self.client is None:
            return
        yield Thinking("The floor takes shape")
        ok = direct_floor(
            floor, self.theme, self.client, self.settings.director,
            previously=self.state.floor_history[-3:],
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
            yield Line(room.concept or _KIND_BLURB[room.kind])
            return

        yield ProseStart(channel="room")
        for piece in self.narrator.room(self.state.floor, room):
            yield ProseDelta(piece)
        yield ProseEnd()

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
