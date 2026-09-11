# M14 — Looting · ✅ COMPLETE

> **Status: done.** 858 tests green (833 before M14). Built to the design as
> approved, except where L7's simulation overruled it. The drop rates, the boss
> rule and the cache frequency were cut to keep the median run within one
> floor, which means **D1 and D2 differ from what was approved.** Findings at
> the bottom. §§1–5 are the design as reviewed, kept as written.
>
> **Since then:** [M14.1](M14.1-fixes-tasks.md) armed floors 1–3, fixing the
> missing vault found below. That moved the base curve two floors, and the
> boss's drop rate came down from 50% to 35% to keep loot within one floor of
> the armed game.

> Scoped from [M13's ledger](M13-open-items-tasks.md): O13 (looting, corpses,
> drops) and the setup for O14 (weapons with tradeoffs). It also answers the
> first playtest's question: *"do we have a plan to add looting and searching
> other things?"*

**Goal:** fights and searches pay off in things you can carry. A dead monster
can leave something behind. A room can hide something a search turns up. A
find on floor 7 is worth more than a find on floor 1.

**Constraint: tier 0.** No new model calls. Every item comes from the theme's
pools through seeded dice, and **the model never names or invents loot**. That
extends two lines that exist for good reason:
- M9: an NPC can only hand over something that actually exists.
- M10: a discovery is scenery, never an item. That stays true here.

---

## 1. What exists today

- **Items come from two places.** Each floor gets one weapon, in its vault
  (`floorgen._populate`). Each other room has a 25% chance of a heal. Both are
  drawn from the theme's `[items]` pools: weapons hit for 3–4 and heals give
  4–6, **at every depth**.
- **Themes define `key` items that nothing ever places.**
- **Floors regenerate every run** from `floor_rng(world_seed, depth)`. Only a
  floor's identity is stored, so a world's items and monsters are the same each
  run, and a taken item is back next run. Loot fits that: run-scoped unless
  stated otherwise.
- **Death ends the run, and the pack goes with it.**
- **NPC holdings are world-scoped.** A gift sits in the NPC's node data across
  runs, and a warm NPC may hand an item back, decided by the model. Nothing caps
  what an NPC holds.
- **Combat swings the best weapon carried.** `equip` reports this rather than
  changing it (M11), because weapons differ only in damage.

---

## 2. What the balance simulation says

M10 held its line — *"the moment a found thing is takeable, this becomes a
loot-balance milestone"* — without numbers. Here they are. I ran M3's greedy
simulation from `tests/test_combat.py` with loot policies added: pick up
everything, drink below 35% hp, fight every room. It's offline, 400 runs per
policy per theme. The two themes agreed within one floor on every row, so this
shows simulacra, with Hardpan noted where it differed.

| policy | median floor | p90 | dead by floor 2 |
|---|---|---|---|
| **today** | 4 | 6 | 3% |
| heal drops at the recommended rates (below) | 4 | 6 | 1% |
| …plus 30% of drops weapons, weapons +8%/floor | 4 | 7 | 1% |
| weapons +15%/floor, nothing else | 4 | 6 | 3% |
| a heal drop on **every** kill | 5 | 7 | 0% |
| every kill drops, half weapons, weapons +15%/floor | 6 | 8 | 0% |
| player defense 14 instead of 12 | 5 | 7 | 1% |
| player attack 4 instead of 3 | 4 | 6 | 1% |

**No run dies holding a heal.** That's 0% in every policy at the recommended
rates, on both themes. Heals are always spent: the player is outscaled, not
under-supplied.

**The curve is set by the monster tiers and the lair boss, not by loot.** At the
recommended rates the median doesn't move and early deaths fall. It takes a
heal on every kill, or two points of defense, to buy one more floor. It takes
stacking every generous choice to buy two. The dungeon still wins in every
policy; the worst run-end, max 9, never came close to the test's 40.

**Not yet simulated:** three things this design proposes. Heals scaling with
depth (the runs above scale weapons only), hidden caches, and the boss always
dropping (the runs gave strong monsters, the boss included, 60%). L7 simulates
all three together before anything ships. The simulation's own history says
not to assume: M3 found its curve capped at floor 4 only by simulating whole
runs.

So M10's fear was right about what it named, *free healing on demand*: loot
without a bound. Bounded, seeded drops at these rates are balance-safe. M3's
tests remain the guard, and L7 teaches the simulation the new sources so that
it keeps simulating the game that actually exists.

---

## 3. The design

### L1 — A kill can leave something behind

On a kill, roll the run's dice against the monster's tier. Weak monsters drop
25% of the time, normal 40%, strong 60%. **The lair boss always drops, and
leans toward a weapon**, so the vault is no longer the floor's only weapon. A
drop is 70% a heal and 30% a weapon, drawn from the theme's optional `[drops]`
table for that tier, or from `[items]` when the theme has none. It lands in the
room's items, and the player reads *"It leaves behind a canteen, still heavy."*
Take it like anything else.

**One kill path.** A monster dies in `_attack` and in `_improvise` (a judge
ruling of `damage_target`). Both must go through one helper that says "is
finished" and rolls the drop. This is the recurring bug shape: a behaviour
written for one caller, missing from the second.

### L2 — Search can turn something up

`floorgen` hides a **cache** in some rooms: about 20% of chambers, corridors and
shrines, so roughly one per floor. It is invisible to `_contents`, to
`_room_summary` (which the parser fallback lifts targets from) and to the
narrator's census. A bare `search` finds a cache first, at tier 0: *"You find a
tin of peaches wedged behind the timbering."* The cache moves to the room's
items. The search costs a turn and provokes, as a discovery does (M10). After
that, `search` goes on to discoveries exactly as it does today.

This is what makes searching worth doing. The cache is placed by the world, not
invented by the model. Discovered prose stays scenery, and
`test_a_discovery_is_not_takeable` stays.

**Existing worlds must not change.** Caches draw from their own stream, via a
new third argument, `floor_rng(world_seed, depth, "loot")`, never from the
floor's own stream. If
they drew from the floor's stream, every M6 world would regenerate differently.
A golden test pins layout, names, monsters and vault items for fixed seeds
against the pre-M14 generator.

### L3 — Deeper finds are worth more

An item's value scales with depth: a weapon's damage and a heal's amount each
grow +8% per floor. A deep heal keeps pace with max hp, which grows 3 per floor,
and a deep weapon is worth picking up. Values show where they matter: the
`inventory` listing reads *"a bent single-jack (hits for 5)"*, *"a tin of
peaches (heals 6)"*. That fits a game that already prints its rolls. `equip`
already compares weapons by value.

This is also the setup for O14. Once items vary in value, weapons with
tradeoffs become a question worth asking.

### L4 — `take all`, and `loot` that loots

`take all` and `take everything` take everything visible. `loot` takes
everything when there is anything to take, and searches when there isn't.
Today `loot` is only an alias for `search` (M11).

### L5 — The dead leave their packs (bones)

When a run ends, **up to three of the best things the delver carried** stay in
the room where they died, as world state on that room's node. The next run to
reach that room finds *"a delver's pack"*, and `take all` empties it. There is
**one pack per world**: the next death replaces it, and a taken pack is gone.

It's the most on-theme thing loot could do. This is a game about what the dead
leave behind, and the memory layer already remembers *who* died there. It is
also the first item state that outlives a run. It's written by merging into
the room node's data, next to M10's `found` index, never by replacing it: the
M11 lesson.

The obvious exploit is to die with your pack on purpose. The bound: you can
only bank what you found, and the next run has to reach that floor to collect
it.

### L6 — NPCs can't be banks

An NPC holds at most three items. A gift beyond that is refused at tier 0, in
voice, before any model call: *"My hands are full."* Without the cap, drops,
caches and bones all feed a world-scoped store that a warm NPC can hand back.

### L7 — Measure

- **Offline.** `simulate_run` learns drops and caches, so M3's curve tests run
  against the game as it actually is. New bound: the median stays within one
  floor of today's 4.
- **Live.** A playtest with a transcript. Count drops, caches found, items
  taken, and whether anything in the room text contradicts the room's item
  list.

---

## 4. Decisions for you

| # | Decision | Recommendation | Why |
|---|---|---|---|
| D1 | Drop rates | **25 / 40 / 60%, boss always** | The median doesn't move; early deaths 3% → 1% |
| D2 | Hidden caches that search reveals | **Yes, ~1 per floor** | Makes `search` pay; placed by the world, never by the model |
| D3 | Values scale with depth, and show in `inventory` | **Yes, +8%/floor** | Without it a second weapon is never worth picking up |
| D4 | Bones: the last death's pack stays in the world | **Yes, capped at 3, built last** | The most on-theme loot there is; the only cross-run item state |
| D5 | Cap NPC holdings | **Yes, 3** | Otherwise loot flows into a world-scoped bank |

**Deliberately not in M14:**
- **Armor and defense items.** Two points of defense buy as much as a heal on
  every kill. They get their own measured pass.
- **Keys and locks.** Themes define keys, but a lock is new persisted room
  state, and a mechanic of its own.
- **Weapons with tradeoffs (O14).** L3 sets it up; it's the next question, not
  this one.
- **Discoveries becoming items.** M10's line holds.
- **Currency and selling.** The Assayer weighing what you bring up is a
  tempting hook for later.

---

## 5. Tasks, in build order

| Task | Files | Test first |
|---|---|---|
| L3 values scale; `inventory` shows them | `floorgen.py`, `loop.py` | a floor-8 weapon hits harder than a floor-1 one; the listing shows it |
| L1 drops, one kill helper | `loop.py`, `theme.py` (`[drops]`) | a seeded kill drops; a judge kill drops too; the boss always drops |
| L2 caches, own stream | `floorgen.py`, `state.py`, `loop.py` | a golden floor is unchanged; a cache is absent from contents, summary and census; bare `search` finds it once |
| L4 `take all` / `loot` | `parser.py`, `loop.py` | `loot` takes when there is something, and searches when not |
| L6 NPC cap | `dealings.py`, `loop.py` | a fourth gift is refused with no model call |
| L7 sim and bounds | `tests/test_combat.py` | the curve tests pass with the new sources; median within one of 4 |
| L5 bones | `state.py`, `loop.py`, `store.py` | a death leaves ≤3 items; the next run finds them; taking empties it; the `found` index survives |
| measure | — | a live playtest, and the transcript reviewed |

## Definition of done

- [x] A kill can drop, by every kill path. The boss drops more often than any
      tier: 50%, where the design said always
- [x] A bare `search` reveals a cache once; a cache is never listed, summarised or narrated before that
- [x] Existing worlds regenerate identically: an 80-floor snapshot, plus a
      stream test that doesn't depend on the theme files
- [x] Item values grow with depth and show in `inventory`
- [x] `take all` and `loot` work
- [x] No NPC holds more than three items
- [x] M3's curve tests pass against the loot-aware simulation; the median
      moves from floor 4 to 5
- [x] The last death's pack waits in its room, and only one pack exists per world
- [x] No model call anywhere in M14's paths: tested offline, and the NPC-cap
      test counts calls
- [x] Played end to end, scripted and offline, since loot needs no model. A
      human playtest is still to come

## Watch for

- **A kill path that doesn't drop.** Attack and improvise both kill; followers
  might one day.
- **Hidden meaning seen.** A cache named in `_room_summary` is a target the
  parser fallback will lift. That is the M11.1 bug again.
- **The floor stream.** One extra draw in `_populate` silently regenerates
  every existing world's monsters and vault items. Caches get their own stream,
  and the golden test proves it.
- **Node blobs replaced rather than merged.** Bones share `room:` node data with
  `found`. That makes four bugs of this shape so far, and it would be a fifth.

---

## Findings

### The design failed its own bound, and L7 is why we know

§2 warned that three of the proposals had never been simulated. Simulated
together, the whole design moved the median run **from floor 4 to floor 7** on
both themes. Isolated, 200 runs each:

| loot | median floor |
|---|---|
| none | 4 |
| caches only (20% of eligible rooms) | 5 |
| drops only, boss at its tier's rate | 5 |
| **the boss always drops, nothing else** | **6** |
| everything, as designed | 7 |

Weapons were not the driver. A boss whose drop was always a heal moved the
curve exactly as far as one leaning toward a weapon. Each source alone was worth
a floor or two, and together they pushed runs up to the wall around floors 7–8,
where the strong tier arrives: p90 was 7–8 in every configuration, and the
maximum 9. The boss rule was the costliest single choice. A heal right after
the floor's hardest fight, every floor, is exactly when a heal is worth most.

### What shipped

This is the most generous point on a grid of nine that held the bound on both
themes, at 200 and 400 runs:

| | designed | shipped |
|---|---|---|
| drops, weak / normal / strong | 25 / 40 / 60% | **15 / 25 / 40%** |
| the lair boss | always drops | **50%**, and still leaning toward a weapon |
| caches | 20% of eligible rooms, ~0.8 a floor | **10%, ~0.4 a floor** |
| median run (4 without loot) | 7 | **5** |

**Decisions D1 and D2 changed from what you approved.** The bound you approved
along with them decided it. Of the grid, the closest to the approved rates was
the designed drop rates with 10% caches and no boss rule. It held on simulacra
and broke on Hardpan at 200 runs, where the median reached 6.

### Floors 1–3 have no vault

I found this while looking for the driver. On floors of 5–6 rooms the vault's
slot and the shrine's slot are the same room, and the shrine wins. So floors
1–3 have no vault and no weapon: every run fights bare-handed until floor 4,
unless something drops. §1 of this doc said every floor has one weapon, which
was wrong for the first three.

It's not fixed here. Changing which room gets which role changes the rooms of
every existing world, whose stored names and concepts were written for the old
roles. It's recorded as M13 ledger item O18. The boss's lean toward a weapon is
what M14 does about it.

### Existing worlds regenerate as they were

I snapshotted 80 floors before any M14 edit: both themes, four world seeds,
depths 1–10. Every one regenerates identically, with the loot stream and
without: layout, names, monsters and item ids. Only item values moved, and
each by exactly the designed scale. `test_the_loot_stream_does_not_move_the_floor`
keeps it that way without depending on the theme files.

### Deviations from the design

- **Bones live on their own node, `bones:last`,** not the room node. One pack
  per world becomes structural, and the room's blob keeps a single writer.
- **The inventory says *"(damage 5)"*, not *"hits for 5"*.** A weapon's number
  is the base of a damage roll (1 to base + 2), not a hit.
- **`loot the offcut`** used to say *"it left nothing behind"* (M11), because
  the dead carried nothing. Now they can, so `loot` takes what lies here,
  named or not.

### Played end to end

Loot is tier 0, so all of it can be played without a model. I scripted a
greedy player on fresh Hardpan worlds: walk every room, fight, `search`,
`take all`, descend.

- **Kills dropped, searches found caches, `take all` took both.** For example:
  *"a dry hand is finished. It leaves behind a canteen, still heavy."* and
  *"You turn up a tin of peaches."*
- **Eight lives in one world.** The pack moved with each death: floors 5, 3,
  2, 3, 2, 5, 4, 5. Run 8 found it on floor 4, where run 7 died, and emptied
  it.
- **Bones reward getting back as deep as the last delver, which is rarer than
  it sounds.** Five of those lives had a full pack waiting, and one reached it.
- **Worth deciding after playing: two deaths carrying nothing left an empty
  pack, wiping a full one.** That is the design, since the pack is the last
  delver's rather than the best there has been. But it makes a pack fleeting.
  Keeping the old pack when the new death carries nothing is one line to
  change.
