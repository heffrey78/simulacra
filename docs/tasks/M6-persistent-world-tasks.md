# M6 — Persistent world + reset

> Scoped from [systems.md §4](../plan/systems.md#4-s0--the-persistent-world).
> First milestone after the POC. It contains no model calls and adds no prose —
> it is the durable-state foundation M7–M10 all write into, and it goes first
> because a schema change is cheapest before another world's worth of canon
> accumulates.

**Goal:** floor 7 is the same floor 7 on every run of a given world; the world
has an identity of its own; and there is a first-class way to start a new one or
retire an NPC that has gone bad.

**Constraint that shapes all of it:** tier 0, throughout. Nothing here may add a
model call to any path. The one latency change it *should* produce is a
reduction — the director's ~19 s tier-3 call becomes once per floor per world
instead of once per floor per run (W6).

---

## Two bugs this milestone has to fix on the way

Both were found reviewing the seed handling for W2, and both are load-bearing
for persistence rather than incidental.

**Floor layout is coupled to combat dice from floor 2 onward.**
[state.py](../../src/simulacra/engine/state.py) documents the invariant — *"the
state RNG and the floor RNG are separate streams off the same seed, so that
in-game rolls can never shift floor layout (or vice versa)"* — and honours it for
floor 1, which generates from `Random(seed)`. But
[loop.py](../../src/simulacra/engine/loop.py) `descend()` generates every
subsequent floor from `self.state.rng`, which *is* the dice stream. So how many
attacks you rolled on floor 1 decides the shape of floor 2. Persistence makes
this fatal rather than merely surprising: the same world seed would produce
different floors depending on how a run played out.

**Floor 1's graph node never gets its real name.** `persist_floor` runs inside
`new_run`, before `begin()` calls `_direct()`. Floors 2+ are fine — `descend()`
deliberately directs before persisting ("so the floor node carries its real
name") — but `floor:1` is written as `"floor 1"` with an empty `theme_name` and
is never updated. Invisible today because nothing reads floor nodes back. W3 is
the milestone that starts reading them back.

---

## W1 — The `world` table

**Files:** [memory/store.py](../../src/simulacra/memory/store.py)

Add to `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS world (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    world_seed     INTEGER NOT NULL,
    theme          TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    created_at     REAL NOT NULL
);
```

Plus `SCHEMA_VERSION = 1` as a module constant, and two methods:

- `Store.world()` → the row, or `None` for a database that has never held one.
- `Store.create_world(seed, theme)` → writes row 1. Raises if one exists.

**Creation must be automatic and lazy.** Every test in the suite constructs a
`Store` against a `tmp_path` and none of them should have to know this table
exists. `Store.__init__` therefore ensures a world row the same way it ensures
the schema — if there is none, create one with a random seed and the settings
theme. `--new-world` (W4) is an explicit override of an existing world, not the
only path that makes one.

**Version mismatch must report, not crash.** On open, compare the stored
`schema_version` to `SCHEMA_VERSION`:

- equal → proceed.
- stored is *lower* → no migrations exist at M6. Refuse with the exact
  `--new-world` invocation that archives it, in the style of `client.health()`
  naming the exact `ollama pull`.
- stored is *higher* → the database was written by a newer build. Refuse and say
  so; do not open it read-only and pretend.

The whole point of the column is that a world someone has invested twenty runs
in can *detect* an incompatible build. A stack trace against a changed table is
the failure mode it exists to prevent.

**Tests:** a fresh `Store` has exactly one world row; a second `create_world`
raises; a row with a bumped version refuses to open with a message naming
`--new-world`; existing tests keep passing untouched (the proof that creation is
genuinely lazy).

---

## W2 — One seed per job

**Files:** [engine/state.py](../../src/simulacra/engine/state.py),
[engine/loop.py](../../src/simulacra/engine/loop.py),
[memory/store.py](../../src/simulacra/memory/store.py)

Today one `seed` drives both floor layout and dice. Split it:

| | Source | Decides |
|---|---|---|
| **world seed** | `world.world_seed`, written once | layout at every depth |
| **run seed** | derived per run from `run_id` | dice only |

Every floor generates from the world seed and its own depth:

```python
generate_floor(depth, theme, random.Random(world_seed ^ depth))
```

in **both** call sites — `new_run` for floor 1 and `descend()` for the rest.
That is the fix for the dice-coupling bug above; `state.rng` stops appearing in
`generate_floor` entirely, which is worth a test asserting directly.

**`runs.seed` changes meaning** from floor seed to run seed. The column stays;
what it records does not. Note it in the schema comment — the existing comment
("Recorded so any run can be replayed exactly") becomes half true and needs to
say which half.

**`--seed` becomes context-dependent, and this must be stated in `--help`:**

- with `--new-world`, or against a database with no world → sets the **world**
  seed. This is the "give me this specific dungeon" case that exists today.
- against an existing world → sets the **run** seed, and prints a one-line note
  saying the world seed is fixed and where it came from. Silently ignoring it
  would be worse than either alternative.

**Tests:** two runs against the same `Store` produce identical floors at every
depth 1–5; a run whose dice stream is advanced before descending still produces
the same floor 2 (the regression test for the coupling bug); floor layout is
unchanged by `run_id`.

---

## W3 — Recompute layout, re-attach identity

**Files:** [engine/state.py](../../src/simulacra/engine/state.py) (`persist_floor`),
[engine/loop.py](../../src/simulacra/engine/loop.py) (`descend`, `_direct`),
[memory/store.py](../../src/simulacra/memory/store.py)

**Do not serialise the floor graph.** `floorgen` is deterministic and runs in
microseconds; regenerating floor 7 from `world_seed ^ 7` is cheaper than reading
it back, and it keeps `floorgen` the single source of truth for structure. What
cannot be regenerated is what the model contributed:

| Stored | Where |
|---|---|
| `floor.theme_name`, `goal`, `motifs` | `floor:<depth>` node `data` |
| `room.name`, `room.concept` | `room:<id>` node `data` |

So the floor lifecycle becomes: **generate → look for stored identity → apply it
if present, otherwise call the director and store what it returns.**

Two consequences to get right:

- **Persist after directing, always.** Fix the floor-1 ordering bug by moving
  floor 1's `persist_floor` out of `new_run` and behind the director pass in
  `begin()`, so both paths write identity that exists. `new_run` keeps building
  the floor; it stops writing it.
- **`_direct` becomes conditional.** A floor with stored identity must not call
  the director at all — no `Thinking` event, no 19 s spinner, no
  `floor_history` append. That is the whole payoff of the milestone and it needs
  a test asserting zero tier-3 calls on a second visit.

**`floor_history` needs care.** It feeds the director a do-not-repeat list of
recent floor names, and it currently accumulates per run. With floors persisting,
the list should come from the world's already-named floors (a `nodes` query for
kind `floor`), not from this run's traversal — otherwise the first descent of run
2 tells the director to avoid nothing and it may re-invent a name the world
already uses two floors up.

**Free win, worth verifying rather than assuming.** `Narrator._key` is
`prose:{room.id}:sha1(theme|concept|floor.theme_name)` and room ids (`d7r3`) are
already stable across runs. Once concepts are stored and re-applied, that key
becomes stable too and **cached room prose starts surviving across runs** with no
further work. Confirm it does; it is a large part of what makes a revisited world
feel instant.

**Also fixed by this, quietly:** today `room:d1r0` in the graph means a different
room on every run, so the existing edges already collide across runs. Persistence
gives node ids a stable referent for the first time, which is the precondition
for M7 hanging canon off them.

**Tests:** run 2 of a world produces byte-identical floor names, goals, motifs
and room concepts to run 1; the director is called zero times on run 2; a floor
generated with no stored identity still calls the director exactly once;
`floor_history` on run 2's first descent contains run 1's floor names.

---

## W4 — Reset

**Files:** [\_\_main\_\_.py](../../src/simulacra/__main__.py),
[memory/store.py](../../src/simulacra/memory/store.py)

Three operations, deliberately different in scope.

**`--new-world`** — rename `saves/world.db` to `saves/world-<ISO timestamp>.db`
(with its `-wal` and `-shm` siblings), then create a fresh world. **Archive,
never delete.** The premise of a persistent world is that losing it costs
something, and disk is not the constrained resource on this box. Print the
archive path; a player who did this by accident needs to be told exactly what to
rename back.

Runs before the `Store` is opened, so it must handle: no existing file (just
create), a locked file (report, do not clobber), and an existing archive name
(suffix, do not overwrite).

**`--forget <npc>`** — set `status='retired'` on that NPC's `derived` canon and
delete its episodic memories, keeping `authored` canon and the node row itself.
The escape hatch M7 makes necessary: derived canon is generated, permanence means
a bad generation is permanent, and retiring one NPC is a far better answer than
discarding a world. Ships in M6 rather than with M7 because M7 is the milestone
that starts producing content that might need retiring — the lever should exist
before the thing it controls does.

At M6 there is no `canon` table yet, so the implementation is the memory half
plus the vector rows, and M7 extends it. Say so in the docstring rather than
leaving a half-feature that reads as unfinished.

**`--db <path>`** — unchanged. It is already the multi-world mechanism and needs
no new surface.

**Tests:** `--new-world` on an existing db leaves an archive and a fresh world
with a different seed; twice in the same second does not overwrite the first
archive; `--new-world` with no existing db just creates one; `--forget` removes
that NPC's memories and leaves another NPC's intact.

---

## W5 — Wiring

**Files:** [\_\_main\_\_.py](../../src/simulacra/__main__.py)

`build_session` opens the `Store`, then reads the world row and passes the world
seed into `new_run`. `--theme` against an existing world whose stored theme
differs is a real conflict: the theme decides room name pools and the NPC roster,
so the stored floors would be re-labelled from a different vocabulary. Refuse,
and name `--new-world` — the same shape as the version mismatch in W1.

Keep the exit line honest. It currently prints `replay with: simulacra --seed
<n>`, which after W2 means something different. It should name the world seed and
the world file, since those together are what actually reproduce a dungeon now.

---

## W6 — Measure the payoff

**Files:** a note appended to [plan.md §9](../plan/plan.md#9-milestones), in the
manner of M2's latency table.

The claim to check is that the director cost moves from per-run to per-world.
Two runs of the same world, three floors each, with `--stats`:

| | run 1 | run 2 (expected) |
|---|---|---|
| tier 3 calls | 3 | **0** |
| tier 2 calls | ~15 | **~0** (prose cache, W3) |
| wall time to floor 3 | baseline | materially lower |

If tier 2 on run 2 is *not* near zero, the prose cache key is not stabilising and
W3's "free win" assumption is wrong — investigate before M7 rather than
carrying it as an assumption into a milestone that depends on stable node ids.

---

## Order and dependencies

W1 → W2 → W3 is a hard chain: the world row holds the seed, the seed decides the
layout, stored identity attaches to the layout. W4 depends only on W1. W5 depends
on all of them. W6 is last by definition.

Suggested order: **W1, W2, W4, W3, W5, W6** — W4 before W3 because W3 is the
largest task and being able to reset a world by hand makes iterating on it much
easier.

## Definition of done

- [ ] Floor 7 is the same floor 7 on run 1 and run 5 of a world — layout, name,
      goal, motifs and room concepts
- [ ] Floor layout is provably independent of dice: advancing the combat RNG
      before descending does not change the next floor
- [ ] Run 2 of a world makes **zero** tier-3 director calls
- [ ] `schema_version` mismatch reports the exact command to run, and never
      raises against a changed table
- [ ] `--new-world` archives rather than deletes, and says where
- [ ] `--forget <npc>` removes that NPC's episodic memories and no one else's
- [ ] `--seed` does something defined and *stated* against both a new and an
      existing world
- [ ] The full suite passes with no test having to learn about the `world` table

## Watch for

**Lazy world creation is the thing most likely to break the suite.** Roughly
every test constructs a `Store`. If W1's automatic creation is anything other
than silent and free, this milestone turns into a test-wide refactor for no
gain. If it does, that is the signal to make world creation explicit at the two
real call sites instead — not to edit a hundred tests.

**The dice-coupling fix changes every existing seeded floor.** `--seed 42` will
not produce the dungeon it produced before M6, and several tests assert on
generated layout. That is correct and unavoidable, but it means a diff full of
changed fixtures — re-derive them, and be suspicious of any that *didn't* change,
because a floor unaffected by the fix may be one still generating from the wrong
stream.

**Do not let "recompute layout" quietly become "serialise the floor."** The
temptation arrives the first time a stored concept fails to line up with a
regenerated room id. The right response is to find why the id moved — almost
certainly the seed — not to start writing rooms to disk. `floorgen` staying the
single source of structure is what keeps the world file small and the invariants
testable without a model.

**A persistent world makes old bugs permanent.** Anything the director wrote
badly on run 1 is now what that floor is called forever. That is the intended
trade and `--forget`/`--new-world` are the mitigations, but expect it to feel
worse in play than it reads here, and resist adding a "reroll this floor" verb
in M6 — it belongs with M7's canon amendment, where retirement already has a
mechanism.
