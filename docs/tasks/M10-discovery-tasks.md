# M10 — Lazy world expansion · ✅ COMPLETE

> **Status: done.** 684 tests green with the daemon unreachable. Findings —
> including a field declared in M1 that nothing had ever written to — below.

> Scoped from [systems.md §9](../plan/systems.md#9-s5--lazy-world-expansion).
> **Depends on M6** (a room that persists is a room worth remembering something
> about) and **M7** (canon is where a discovery goes). Last milestone in the
> plan.

**Goal:** the narrator mentions a broken altar, and `look at the altar` finds
something — the same something, three runs later.

**Why this is the one that finishes the arc.** M6 made a world cheap to re-walk,
and immediately raised the question of what makes a second run interesting. M7
and M8 answered it for NPCs. This answers it for rooms.

**Constraint:** tier 2, streamed, and only on a genuine discovery. `look` is
free today and stays free for everything already known.

---

## Where discovery comes from

The obvious design is to ask the model whether a thing is plausible here. That
puts a tier-1 call on `look`, which is the one verb documented as costing
nothing, and it asks a 1.7b a question it has no grounds to answer.

The better source is sitting there already: **the director's concept line.** It
has been writing sentences like *"There is a cracked altar and a single glowing
crystal"* since M2, and the narrator expands them into prose the player reads —
and then `look at the altar` says *"You don't see altar here."* The room already
told the player what is in it and the engine disagreed.

So plausibility is tier 0 and comes in two grades:

| Grade | Source | Gate |
|---|---|---|
| **mentioned** | the room's concept, prose or name | always found |
| **fixture** | the theme's per-`RoomKind` pool | RNG, once per room |
| absent | neither | "You don't see that here" |

The first grade is the feature. The second is what keeps a room rewarding
someone who pokes at it beyond what was written down.

---

## D1 — Room canon and the budget

**Files:** [memory/store.py](../../src/simulacra/memory/store.py),
[world/theme.py](../../src/simulacra/world/theme.py),
[themes/simulacra.toml](../../themes/simulacra.toml)

Discoveries are canon on the `room:` node — the table M7 built, used for the
first time by something that is not an NPC. `provenance='derived'`, because that
is exactly what it is: generated from what the world already established.

The theme gains a `[fixtures]` table, per `RoomKind`, in the same shape as
`[rooms]` and `[monsters]`:

```toml
[fixtures]
vault  = ["shelves", "a strongbox", "ledgers", "seals"]
shrine = ["an altar", "offerings", "a basin", "carvings"]
```

Structure is code's, names are the theme's — the same rule as everywhere else.

**The budget lives in code.** `DISCOVERY_BUDGET` per room: without it a player
grinds infinite content out of one room and the dungeon stops meaning anything.
Code owns how many; the model owns what.

**Tests:** the fixture pool loads; canon written against a `room:` node comes
back for that room and not another; the budget counts discoveries and not looks.

---

## D2 — Plausibility, in code

**Files:** new `world/discovery.py`

```python
def grade(target, room, theme) -> "mentioned" | "fixture" | "absent"
```

Pure, no store, no model, no I/O — the same shape as `floorgen`. `mentioned`
matches against the room's `concept`, `prose` and `name`; `fixture` against the
theme's pool for the room's kind. Word-level matching, not substring: "altar"
must not match "alteration".

**Fixtures roll once per room, not once per look.** A room's fertility is a
property of the room, so the roll is seeded from the room id and the world seed
— re-rolling per attempt lets a player retry until it lands, which is the
grind the budget exists to prevent.

**Tests:** a concept noun grades `mentioned`; a pool noun grades `fixture`; a
word in neither grades `absent`; grading is stable across repeated calls; the
roll is stable for a given room and world.

---

## D3 — Finding it

**Files:** [engine/loop.py](../../src/simulacra/engine/loop.py) (`_look_at`),
[narrate/narrator.py](../../src/simulacra/narrate/narrator.py)

On a `mentioned` or a successful `fixture` roll, and with budget left: one
tier-2 streamed call describes what is there, and the result is written as room
canon.

**A discovery is an action; a look is not.** `look` never sets `_resolved`
today, and eyeballing something should stay free. Rummaging until you find
something is not eyeballing — it takes time, and doing it with something hostile
in the room should cost you a turn. Only the discovery branch resolves.

**The guards apply.** The narrator's echo and degeneracy checks run on this the
way they run on room prose, and — per M8 — a rejected generation must not be
written down. Canon outlives the run; that lesson is M7's and it cost a live
five-run read to learn.

**Tests:** a mentioned target discovers; a rejected generation writes no canon
and reports nothing found; a discovery sets `_resolved` and a plain look does
not.

---

## D4 — The same thing next time

**Files:** `engine/loop.py`

A second `look at the altar` reads the canon row and replays it. No model call,
no new roll, no budget spent — and with floors persisting since M6, that holds
across runs. **A world that rewards re-exploration has to remember what it
showed you**, and a room that invents a different altar every visit is worse
than one with no altar at all.

**Tests:** the second look is byte-identical and costs no model call; it holds
across a new run against the same world; a different world finds a different
thing.

---

## D5 — Exhaustion

**Files:** `engine/loop.py`, `world/discovery.py`

Past the budget, a room is finished: further unknown targets get the ordinary
"you don't see that here", with no roll and no call. Already-discovered things
still describe — the budget caps *new* content, not access to old.

**Tests:** the N+1th discovery is refused with no model call; discovered things
still work afterwards; the budget is per room, not per floor.

---

## D6 — Measure

| | target |
|---|---|
| model calls for a repeated look | **0** |
| model calls once a room is exhausted | **0** |
| tier-2 calls per genuine discovery | 1 |

And the qualitative check, in the manner of M7 and M8: **read a room's prose,
then look at three nouns it used.** If the prose says there is an altar and the
engine says there is no altar, this milestone did not land, whatever the tests
say.

---

## Order and dependencies

D1 → D2 → D3 → D4 → D5. D6 last.

## Definition of done

- [ ] A noun the room's own prose used can be looked at, and finds something
- [ ] The same look, three runs later, finds the same thing
- [ ] A repeated look and an exhausted room both cost zero model calls
- [ ] A rejected generation writes no canon
- [ ] A discovery provokes; a plain look still does not
- [ ] Nothing in this milestone can generate an item the player can take

## Watch for

**Discoveries becoming loot.** A found thing is scenery with a description. The
moment it becomes a takeable item it is a balance problem — free healing on
demand — and M10 turns into a loot-generation milestone. Scenery only, and say
so in the code.

**Word matching that is too loose.** "altar" matching "alteration", or a
one-letter target matching everything. Match on words, and set a minimum length.

**Re-rolling until it lands.** If fertility rolls per attempt rather than per
room, a player types `look at shelves` four times and eventually gets shelves.
Seed it from the room.

**The prose/concept gap.** The concept is the director's sentence; `room.prose`
is what the narrator wrote from it, and the player has read the *prose*. Grade
against both, or the nouns the player actually saw will not work.

---

## Findings

### `Room.prose` was declared in M1 and never written to

The Watch-for at the bottom of this document said: *"The concept is the
director's sentence; `room.prose` is what the narrator wrote from it, and the
player has read the prose. Grade against both, or the nouns the player actually
saw will not work."*

It turned out `Room.prose` — and the `Room.described` property beside it — have
existed since M1 and **nothing has ever assigned to either**. The narrator's
text lived only in `prose_cache`, keyed by a hash of theme, concept and floor
name. So grading against the prose was grading against an empty string, and the
first live run showed it: `look at the door` worked because "door" was in the
*concept*, while every noun the narrator had introduced did not.

`_describe` now records what it streamed. That is what the field was for.

Third time in five milestones that the fix was wiring something that had been
declared and left dangling — `Store.neighbors` (unused until M8), `disposition`
and `inventory` (unused until M9), and now `Room.prose`. Writing the slot early
is cheap and has repeatedly been right; the cost is that "it exists" and "it
works" drift apart silently.

### The repeat lookup matched on the wrong thing

A second look was resolved by intersecting the target's words with the *text* of
each stored discovery. Measured live: `look at the surface` replayed the door,
because the door's description happened to contain the word "surface". Anything
sharing a word with a description became an alias for it.

Discoveries are now indexed on **what was searched for** — every word of the
player's target maps to the canon row it produced, in the room node's `found`
map — so `the cracked altar` and `altar` both reach the same find and `smooth`
reaches nothing.

### A regex cannot tell a noun from a verb

`look at the under` found *"a patch of moss and dust"*. A preposition passed the
length floor and appeared in the room's own prose, so it graded as `mentioned`.

The function-word list is now thorough — articles, prepositions, auxiliaries,
degree adverbs. What it still does not catch is **verbs**: `look at the groans`
finds something, because "groans" is a five-letter word the prose used. Short of
part-of-speech tagging it will not, and a heuristic that guesses wrong in either
direction is worse than an odd answer to odd input. Recorded rather than
papered over.

### The find has to be about the thing

The first live pass produced three discoveries in one room that were all
paraphrases of the room description — *"The passage stretches..."*, *"The passage
narrows..."*, *"The walls are cracked..."* The prompt said "the player searches
this room and finds the X", and the model heard "describe this room".

Tightened to *"Write one or two sentences about the X itself — not about the
room, and not about anything else in it"*, which produced a doorway that is
about the doorway. Same lesson as M7's canon inputs: on a small model, what you
point at matters more than how you phrase the instruction.

### The measurement

| | target | measured |
|---|---|---|
| model calls for a repeated look | 0 | **0** |
| model calls once a room is exhausted | 0 | **0** |
| tier-2 calls per genuine discovery | 1 | **1** |

And the qualitative check — a room's own prose, then its nouns:

```
--- the Threshold ---
  The doorway groans under the weight of its own history, its frame warped and
  pitted. The mirror shatters, spilling a thousand faces into the air...

> look at the doorway
  The doorway creaks open, its surface cracked and pitted, like the skin of
  something long dead. The edges are warped, and the frame is bowed, as if it
  had been dragged through a wound.
```

The engine and the prose agree about what is in the room, which is the whole
milestone.

### Known limitations

**Verbs and adjectives are findable.** See above. Bounded by the budget, and the
answer is at least in voice.

**A discovery is scenery, never an item.** Deliberate, tested, and the line to
hold: the moment a found thing is takeable, this becomes a loot-balance
milestone and free healing on demand.

**Discoveries do not reach the `room` route yet.** They are canon on the `room:`
node, so an NPC asked about this place could in principle mention what you
found. The resolver reads only the node's `concept`. One line to change, and
worth doing when there is a reason to rather than on the way past.
