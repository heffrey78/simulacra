# M2 — It reads like a game

> Milestone from [plan.md](../plan/plan.md) §9. **Status: ✅ complete.**
> 270 tests green, all without a daemon. Written as a record after execution.

**Goal:** the model writes the world. Streamed room prose, one floor-identity
call per floor, and generate-ahead so the player never waits for either.

**Result: the latency budget holds.** Movement turns cost 0.00 s. See §9 of the
plan for the numbers.

---

## What was built

### Narrator — [narrate/narrator.py](../../src/simulacra/narrate/narrator.py)

Tier 2. Streams room prose; caches to SQLite so a revisit is free.

**The echo guard is the substance of this task.** M0 benchmarking caught
`qwen3:1.7b` returning the room census back verbatim instead of narrating —
intermittently, from a prompt that had worked moments earlier. An intermittent
instruction-following failure can't be prompted away with confidence, so output
is checked:

1. The census is **labelled** (`ROOM:`, `EXITS:`) so it reads as data rather than
   prose to continue — and so an echo is trivially detectable.
2. Only the first **48 characters** are buffered before streaming is released. An
   echo starts wrong immediately, so the prefix check costs ~3 tokens of delay
   instead of a whole generation.
3. On rejection: one strict retry, then the procedural fallback. The player never
   sees an error.

Cache key includes the room concept, so a director rename invalidates stale prose
describing the old concept.

### Prefetcher — [narrate/prefetch.py](../../src/simulacra/narrate/prefetch.py)

One worker thread, strictly serial. Narrates the current room's neighbours while
the player reads.

**Why the narrator streams even when it wants a whole string:** `OllamaClient`
serializes requests behind a lock, so a prefetch that ran to completion would
block the foreground for a full generation no matter how promptly it was asked
to stop. Streaming lets the worker drop out between tokens and release the lock
in milliseconds.

**Two flags, not one.** `_cancel` aborts an in-flight generation; `_resume` gates
whether the worker may start a new one. Collapsing them caused the spin bug below.

### Director — [world/director.py](../../src/simulacra/world/director.py)

Tier 3, once per floor. Receives a room census, returns floor name, goal, motifs
and a per-room concept. Corridors are excluded — they're over half a deep floor
and need no identity, so including them would multiply the most expensive call in
the game for nothing.

`apply_floor_plan` is split out from the call so it is testable without a model
and so a partly-malformed response still contributes what it got right. Invented
room ids are dropped.

### Wiring

`Engine._describe()` swapped from `Line` to `ProseStart`/`ProseDelta`/`ProseEnd`
— **the swap touched nothing else**, as M1 designed for. The M1 flat-line path is
kept as the no-model fallback, not deleted, and `--offline` still plays.

---

## Bugs found by running it

**Prefetch worker spun hot.** `Event.wait()` returns *immediately* when the event
is set, so using the cancel flag to mean "wait until preemption ends" busy-looped
a whole core — 3.7 M iterations across four turns, stolen directly from CPU
inference. Fixed with the separate `_resume` event. Regression test:
`test_preemption_does_not_spin`.

**Director overwrote good room names.** Asked for a room name, the model restated
the structural role — "Entrance", "Chamber", "Descent" — replacing the theme
pack's far better pools ("First Landing", "the Unworn Place"). Now rejected by
`_is_generic`; the concept, which is the valuable half, still applies.

**`completed` counted cache hits**, inflating it past the number of model calls
actually made (19 reported vs 12 real). The worker now skips cached rooms without
counting them.

---

## Measurement note

The first two live runs measured a 0% prefetch hit rate, which was **the harness,
not the code**: piping commands with `sleep` buffers them, so once the 20 s
director call put the game behind, every subsequent turn arrived instantly. A
driver that models reading time (4 words/sec, per plan.md §1) showed the true
picture. Worth remembering before trusting any future timing run.

---

## Deferred

- **A new floor's entrance can never be prefetched** — the floor doesn't exist
  until you descend. Eagerly generating floor N+1 when the player reaches the
  descent room would close the last 30% of the hit rate. Not needed for the
  budget to hold.
- Prose runs 45–90 words against a brief asking for forty. Tuning, not a defect.
- `Narrator.npc()` (M4) and `epitaph()` (M3) remain stubs.
