# Simulacra — roadmap

What's next, in one place. [plan.md §9](plan/plan.md#9-milestones) is the
record of what was built; this is the list of what might be. The original plan
ended at M10, and since then every milestone has come from a playtest or a
sweep of open items. This file replaces the sweeps.

## How it works

- **Adding.** Anything goes in **Ideas** as one line: a feature, a nag, a
  question. Say it to Claude or write it here. It gets the next free ID.
- **IDs are permanent.** An item keeps its `R` number through every section and
  after it's done. Numbers are never reused.
- **Sections are status.**
  - *Next*: one to three items, designed or being designed. Picked together.
  - *Your call*: decisions only the designer can make. Nothing in them gets
    built until they're decided.
  - *Later*: known and wanted, not yet designed.
  - *Ideas*: raw. They get shaped when they move up.
  - *Not doing*: with the reason, so they don't come back unexamined.
  - *Done*: one line and a link each.
- **Working an item.** Moving an item to *Next* gives it a milestone number and
  a task doc in [docs/tasks/](tasks/), in the usual shape: goal, constraint,
  design, measurements, definition of done, findings. The roadmap entry links
  to the task doc. When the milestone is done, plan.md §9 gets its summary and
  the entry moves to *Done*.
- **Nothing open gets lost.** Anything a milestone or playtest leaves open gets a
  roadmap entry in the same commit.

---

## Next

Nothing picked yet.

## Your call

### R4 · Is a game two floors longer the right length?
*From:* [M14.1](tasks/M14.1-fixes-tasks.md#arming-floors-13-moves-the-curve-two-floors)

Arming floors 1–3 moved the median run from floor 4 to 6 (7 on Hardpan). The
third playtest died on floor 7, right on it. If it's too long, the knobs are
M3's: monster scaling per floor, the tier bands, or leaving floor 1 unarmed.
Loot doesn't need to move. This interacts with R11: a configurable depth
may settle it differently per world.

### R5 · Should a death carrying nothing wipe a full pack?
*From:* [M14](tasks/M14-looting-tasks.md)

The last delver's pack waits where they died, one pack per world. A delver who
dies empty-handed replaces it with an empty one. That's by design, and it may
not be what a player wants.

## Later

### R3 · NPC dialogue invents numbers and plays its role literally
*From:* [M14.1](tasks/M14.1-fixes-tasks.md#f8-measured-live-no-difference-and-a-bigger-problem)

On `qwen3.5:2b`, the Assayer named a price the canon never gave in 10 replies
of 10, and called itself "a scale" or "a sack of iron". The Widow named herself
"Elara". Two changes look likely, and both need a measured pass: a guard or
instruction against inventing numbers, and a role line that says who the NPC
is as well as what they do.

The epitaph shows the same failure. [M15](tasks/M15-search-and-fixes-tasks.md)
took the numbers out of its prompt, and it stopped counting. But told to call
the delver "they", it said "he" in at least 3 of 10, and invented people: "a
young man named Thomas", "a young girl with silver teeth".

### R6 · The model's word tics: *something, thick, scent*
*From:* [M13 O12](tasks/M13-open-items-tasks.md#still-open-and-why)

These come from no prompt. The lever is each theme's `banned` list, and on a
small model banning one tic tends to trade it for another. It's theme work,
measured with M12's words-per-rooms metric.

### R7 · Weapons that differ in more than damage
*From:* [M13 O14](tasks/M13-open-items-tasks.md#still-open-and-why)

Until weapons trade something off, choosing one isn't a decision. M14's
depth-scaled values set this up. It pairs with R12: tools and crafted weapons
would be the natural place for tradeoffs.

### R8 · NPCs that ask questions back; multi-turn plans
*From:* [M13 O17](tasks/M13-open-items-tasks.md#still-open-and-why)

This is a conversation feature, not a fix, and it needs its own design.

### R9 · Condensing old NPC memories
*From:* [M13 O16](tasks/M13-open-items-tasks.md#still-open-and-why)

No world has built up enough episodic memory to slow recall. Revisit when one
does.

### R10 · Longer model thinking before play; larger models
*From:* [plan §9](plan/plan.md#9-milestones), deferred since M10

Thinking is affordable where nobody is waiting: generating derived canon, or a
floor ahead of the player. Larger models wait on the hardware.

## Ideas

### R11 · Floor expansion, configurable
*From:* your list

- Configurable.
- Set the depth, with infinite as an option.
- Set how far a floor spreads sideways.

*Today:*
- A floor has `min(5 + depth // 2, 12)` rooms, so it stops growing at depth 14,
  with `depth // 2 + 1` loops. Both are hard-coded in
  [floorgen.py](../src/simulacra/world/floorgen.py).
- Depth is already unlimited: `descend` always generates another floor, and
  floorgen's invariants are tested to depth 25.
- There's no final floor and no ending. A floor's goal is shown and never
  checked.

*Questions:*
- Does a finite depth get an ending, and does the goal matter?
- Is it set per theme, per world, or at the command line?
- Wider floors change the balance curve (more monsters and items between
  stairs) and `MIN_TRAVERSAL`'s route lengths, so the simulation needs to cover
  them.
- Floors 1–3's slot collision (M14.1) came from small sizes. A size setting
  has to keep every room role.

### R12 · Crafting
*From:* your list

- Craftable resources seeded on each floor.
- Tools seeded on each floor.
- Recipes.

*Today:*
- Items come only from each theme's `[items]` and `[drops]` pools, through
  `floorgen.make_item`. The model never invents one (M14).
- New floor content comes from its own `floor_rng` stream, so existing worlds
  don't change.
- The delver has no carry limit.

*Questions:*
- Are recipes theme data, like items?
- Could search (R1) or fixtures be where resources turn up?
- Is a tool used up, or kept?
- Every new source of items moves the balance curve, as M14 found.

### R13 · Going back up to earlier floors
*From:* your list

*Today:*
- `up` says "You can't go that way." No room has an up exit.
- Floors persist across runs (M6): the layout comes from the world seed, and
  what the model wrote is stored. So an earlier floor can be rebuilt the same.

*Questions:*
- What does a floor keep when you leave it mid-run: kills, taken items, opened
  caches?
- Do monsters return?
- Does it make the game easier, by retreating to heal, or is that the point?
- Where do you arrive: at that floor's stairs?

### R14 · Keys, locked doors and locked chests
*From:* your list

*Today:*
- Both themes already define keys: Hardpan's `key` pool has "a brass tag off
  the board" and "a blasting key, worn smooth".
- Nothing places or uses them. Floorgen seeds only weapons and heals.
- There are no locks and no containers. The vault room kind is the natural
  first lock.

*Questions:*
- Floors are winnable by construction: the route from entrance to stairs is
  built first. So a lock on that route needs its key reachable before it.
- Is a chest a new kind of thing in a room, or a locked cache?
- One key per lock, or keys by kind?
- It links to R12 (a crafted key or lockpick) and R13 (a key found below that
  opens something above).

## Not doing

- **Resolving contradictory canon.** Decided at the first playtest: hold both.
  [M13 O8](tasks/M13-open-items-tasks.md).
- **Prompting against rare drift in derived canon.** Accepted, and bounded by
  `DERIVED_CAP`. [M13 O15](tasks/M13-open-items-tasks.md#still-open-and-why).
- **Tool calling for actions.** Built and unused on purpose: constrained
  structured output gives the same architecture at the reliability this
  hardware has. [systems.md §8](plan/systems.md#8-s4--action-vocabulary).

## Done

- **R1 · Search adds to the room instead of re-describing it** →
  [M15](tasks/M15-search-and-fixes-tasks.md). From the
  [third playtest](playtests/2026-09-11b.md) and your list.
- **R2 · The third playtest's small fixes** →
  [M15](tasks/M15-search-and-fixes-tasks.md): unexplored exits, `greet`,
  two-word NPC names, grouped inventory, the epitaph.

Everything through M14.1 predates this file. See
[plan.md §9](plan/plan.md#9-milestones).
