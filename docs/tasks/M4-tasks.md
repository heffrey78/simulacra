# M4 — Memory · POC COMPLETE

> Milestone from [plan.md](../plan/plan.md) §9. **Status: ✅ complete — POC done.**
> 402 tests green, all without a daemon. Findings appended at the bottom.

**Goal:** runs stop being independent. A persistent NPC recalls what happened to
the *last* delver — by name, floor and cause — and says so.

**Why this shape:** this is the milestone the project exists for. notes.md led
with embeddings and graphs "for maintaining a record of the world, recalling
conversations, preserving continuity and themes"; everything since has been
scaffolding to make that affordable on a CPU box. The store is already built and
verified (plan.md §5) — M4 is wiring, selectivity and one prompt.

**Non-goals — explicitly deferred:**

| Deferred | Milestone |
|---|---|
| Textual TUI | M5 |
| Tool calling in the loop | M5 |
| Multi-hop graph queries beyond one or two hops | M5 |
| NPCs with goals, quests, or inventory | — |
| Summarising or compacting old memories | — (revisit when the DB is large) |

---

## Already done — do not rebuild

The store was built in M0 and smoke-tested; the events were defined in M1.

- [store.py](../../src/simulacra/memory/store.py) — `remember()`, `recall()`,
  `previous_runs()`, `link()`, `neighbors()`. **`recall()` already does hybrid
  graph-then-vector retrieval and already supports `exclude_run`.**
- `Transcript` and `NpcPresent` in
  [events.py](../../src/simulacra/engine/events.py) — **the renderer already
  handles `NpcPresent`**, including a "(remembers you)" tag.
- `OllamaClient.embed()` — `nomic-embed-text`, 768 dims, matching the
  `memory_vectors` table.
- `Theme.npcs` — a roster of `Npc(anchor, name, role, voice)`;
  [simulacra.toml](../../themes/simulacra.toml) defines `npc:archivist`.
- `Actor.recollections` and `Actor.hostile` in
  [model.py](../../src/simulacra/world/model.py).
- **Death is already recorded**: `_record_death` writes `DIED_IN` and
  `KILLED_BY` edges, and `persist_floor` writes rooms. M1 and M3 pulled this
  forward precisely so M4 would find history already waiting.

---

## The architectural decision to make first

**The write path is an observer; the read path is not.**

`Transcript` exists so the engine can *emit* what's worth remembering and the
memory layer consumes it, without memory becoming a dependency of the turn loop.
That works because writes are allowed to happen late.

Reads are not. Recall has to happen at an exact point — before NPC dialogue is
generated — so the engine queries the store directly (it already holds one).

Concretely: `MemoryWriter` implements the same shape as `Renderer`, and
[__main__.py](../../src/simulacra/__main__.py) fans events out to both:

```python
for event in engine.turn(text):
    renderer.handle(event)
    memory.handle(event)
```

Record this asymmetry in the code, because it looks like an inconsistency until
you know why.

---

## T1 — Put NPCs in the world

**Files:** [world/floorgen.py](../../src/simulacra/world/floorgen.py),
[world/theme.py](../../src/simulacra/world/theme.py)

The roster exists; nothing spawns it. Nothing in the game currently has
`hostile=False`.

**Details that matter:**

- Place roster NPCs as `Actor(hostile=False)` so combat already ignores them
  (`actors_attack` and `_attack` both filter on `hostile`) — **verify that rather
  than assume it**, and add a test that you cannot attack the Archivist.
- **Use the theme's `anchor` as the actor id**, not a generated one. The anchor
  *is* the graph identity; a fresh uuid per run would silently give you a new
  NPC every time and the whole milestone would quietly do nothing.
- Place deterministically and shallowly — the shrine on floor 1, or the entrance.
  An NPC the player meets on floor 6 is one most runs never reach, given the M3
  depth distribution (median 4).
- `_populate` must not also drop a monster on top of them.

---

## T2 — Write memories

**File:** new `src/simulacra/memory/writer.py`

**Details that matter:**

- **Selectivity is the design problem, not the plumbing.** Remembering every turn
  fills the store with "you walked north" and recall returns noise. Start
  narrow — deaths, first arrival on a floor, killing a lair boss, reaching a
  vault or shrine, and NPC conversations — and widen only if recall feels thin.
- Embedding happens on the **background lane**, not the turn loop. The
  [Prefetcher](../../src/simulacra/narrate/prefetch.py) is already a serial
  background worker with preemption; either reuse it or copy its shape. Do not
  add an embed call to the foreground.
- `nomic-embed-text` is a **second resident model**. It is small (274 MB) and
  should coexist with the 1.7 b chat model in 10 GB, but **measure it** — if
  loading it evicts the chat model, every subsequent turn pays a cold load and
  the M2 budget is destroyed. Check `ollama ps` during a run before trusting it.
- Write the memory row even if embedding fails. A memory with no vector is still
  reachable by graph anchor; a lost memory is lost.
- Subjects are graph node ids (`npc:archivist`, `room:d1r3`). This is what makes
  `recall(about=...)` work at all.

---

## T3 — Recall on encounter

**File:** [engine/loop.py](../../src/simulacra/engine/loop.py)

**Details that matter:**

- **`exclude_run=state.run_id` is mandatory.** Without it the Archivist
  "remembers" something that happened four turns ago as though it were a past
  life. This is the single easiest way to make the feature look broken.
- Query = a short string embedded at recall time. One embed call, blocking, but
  tiny. Assign it a tier and record the measurement.
- Two or three recollections, no more. Prompt bloat is real at 4096 context, and
  a small model given six memories starts reciting a list instead of speaking.
- `NpcPresent(remembers=bool(recollections))` — the renderer already shows it.
- **Cold start must work.** On the very first run there is nothing to recall; the
  NPC still has to be worth talking to. Test the zero-memory path first.

---

## T4 — NPC dialogue

**File:** [narrate/narrator.py](../../src/simulacra/narrate/narrator.py) · `npc()`

Tier 2, streamed, same discipline as room prose.

**Details that matter:**

- **Quote recollections verbatim** into the prompt. Don't summarise them; the
  specificity ("died on floor four to a smudged figure") is the entire effect.
- Use `Npc.voice` from the theme pack. The Archivist "speaks in inventory, never
  uses your name; uses your number" — that voice is doing work a 1.7 b model
  can't invent.
- `ProseStart(channel="npc", speaker=npc.name)` — the renderer already prints the
  speaker prefix.
- **The echo guard applies here too.** Reuse `looks_like_echo`; there is no
  reason to think dialogue is immune to the failure room prose had.
- **Do not cache NPC dialogue.** `prose_cache` is keyed for rooms, and a
  conversation that replays verbatim is worse than one that costs 6 s.

---

## T5 — The `talk` verb

**File:** [engine/loop.py](../../src/simulacra/engine/loop.py)

- Currently `Notice("Not yet.")`. Wire it to T3 + T4.
- Talking is a **resolved action** (it should provoke monsters — M3's `_resolved`
  flag), but there is a judgement call: talking while something hostile is in the
  room is arguably suicide by conversation. Decide, and write it down.
- Emit a `Transcript(kind="dialogue", subjects=[npc.anchor])` afterwards so the
  conversation itself becomes recallable. **This is what makes the second
  conversation better than the first.**

---

## T6 — Tests

**Files:** `tests/test_memory_writer.py`, `tests/test_npc.py`, and the
integration test below

- **The integration test is the milestone.** Run 1: die on floor 3 to a named
  monster, with a `FakeClient` and stub embeddings. Run 2, same store: talk to
  the Archivist and assert the prompt sent to the model contains the floor-3
  death. Assert `NpcPresent.remembers is True`. That single test is the POC's
  acceptance criterion, so write it first and let it fail.
- Cold start: first-ever run, `remembers is False`, dialogue still generated.
- `exclude_run` — a memory written this run must not surface this run.
- Selectivity — walking ten rooms produces few memories, not ten.
- Embedding failure — the memory row still exists and is graph-recallable.
- You cannot attack a non-hostile NPC.
- All of it with the daemon stopped, via `conftest.FakeClient` (it already
  returns 768-dim zero vectors from `embed()`).

---

## Order and dependencies

```
T1 NPCs ────┐
T2 writer ──┼──> T3 recall ──> T4 dialogue ──> T5 talk verb
            │                                      │
T6 tests ───┴──────────────────────────────────────┘  (integration test first)
```

## Definition of done

- [x] `pytest` green with Ollama stopped
- [x] Die in one run; in the next, the Archivist refers to that death unprompted
- [x] First-ever run works with an empty store
- [x] No recollection ever comes from the current run
- [x] Embedding runs off the turn loop; movement turns still cost 0.00 s
- [x] `ollama ps` confirms the embed model does not evict the chat model
- [x] Measured tier costs added to plan.md §9

## Watch for

**The demo passing for the wrong reason.** An NPC that says something vaguely
ominous will *feel* like memory. Assert on the prompt contents, not the output —
the model will happily improvise continuity it was never given.

**Recall returning the current run.** See T3. It looks like memory and is a bug.

**Memory volume.** At one memory per few turns, a hundred runs is a few thousand
rows — fine for `sqlite-vec`. If selectivity slips to per-turn, recall quality
degrades long before performance does.

**A second resident model.** See T2. This is the one thing in M4 that can break
M2's measured budget, and it will not show up in any test — only in `ollama ps`
and a slow turn.


---

## Findings from execution

**It works, live.** Run 1 talks to the Archivist and dies on floor 3; run 2, same
database, the Archivist says:

> "The delver on floor 1 asked about the archivist and was killed by a smudged
> figure on floor 3 after two turns."

**The second-resident-model risk did not materialise.** `ollama ps` after a full
run shows both models loaded and staying loaded — `nomic-embed-text` at 571 MB
and `qwen3:1.7b` at 2.0 GB, neither evicting the other. Embedding costs 0.1–1.6 s
and runs off the turn loop anyway. Tier costs held: tier 3 ≈ 18 s, tier 2 ≈ 3.5 s,
epitaph 2.6 s.

**Three failures found by running it, none of which tests would have caught.**

1. **The NPC degenerated into a form.** Its first live reply was the theme's
   motif list with `1234567890` written against every entry. A comma-separated
   motif list reads to a small model as a form to fill in. Fixed by dropping
   motifs from dialogue prompts (`style_note(motifs=False)`) — they are
   scene-dressing vocabulary for description, not speech — and by adding
   `looks_degenerate()`, the echo guard's sibling, which catches repetition
   collapse rather than prompt copying. It guards room prose too.

2. **The NPC was being fed its own words.** The dialogue transcript quoted the
   reply verbatim, so the next run's recall contained the Archivist quoting
   itself. Three runs in, two of three recollections were self-quotation
   crowding out an actual death. Transcripts now record only what the *delver*
   brought.

3. **The recall query was biased toward chatter.** Phrased as "who spoke with
   X", it ranked conversations above deaths — those memories are literally about
   speaking. Re-phrased around outcomes, and deaths are now sorted last, where a
   small model attends hardest.

**A concurrency bug in `Store`, found by a test.** One SQLite connection shared
between the turn loop (writing memory rows) and the embedding worker (attaching
vectors), with no lock: sqlite3 serializes statements but not transactions, so
interleaved commits dropped each other's work — 5 vectors for 6 memories. Fixed
with an `RLock` inside `Store`, so every caller is safe regardless of thread
rather than each one having to remember.

**The witness rule.** A memory is subject to whichever NPCs the player has
actually *spoken to* that run (`GameState.met_npcs`). That is what decides who
can recall it. The consequence worth knowing: an NPC you never talk to learns
nothing, so the Archivist only remembers runs in which someone stopped to speak.

## Known limitations

- **Duplicate memories are not deduplicated.** Two runs that each approach the
  Archivist write two near-identical dialogue rows, which eat recall slots.
  Worth a similarity check before writing once the DB is larger.
- Recall is one embed call plus one query, blocking, on the talk turn only.
  Measured at 0.1–1.6 s; not assigned a formal tier.
- Only one NPC exists. The roster mechanism supports more; the theme defines one.
