# M7 — Canon

> Scoped from [systems.md §5](../plan/systems.md#5-s1--canon). **Depends on M6**
> — canon hangs off node ids, and node ids do not have a stable referent until
> floors persist ([M6 tasks](M6-persistent-world-tasks.md), W3).

**Goal:** an NPC has a backstory that is not a death. The Archivist can say
something true about itself, about another NPC it knows, and about something a
previous delver told it — and none of that comes from a `Transcript` written by
`_die()`.

**Why this shape:** M4 diagnosed its self-quotation bug as *don't store replies*
and fixed it by storing nothing, which is why the only content-bearing memory in
the world is a death worded by code ([systems.md
§1](../plan/systems.md#1-why-the-conversation-work-stalled)). M7 replaces that
with typed knowledge, so the loop is closed by **provenance** rather than by
starvation.

**Constraint that shapes all of it:** the derived-canon call is tier 3 (~25–30 s,
`Settings.director` budget). It must never run on the turn loop, and it must not
run at all for an NPC whose episodic memory has not grown. Everything else in
this milestone is tier 0.

---

## Non-goals — explicitly deferred

| Deferred | Milestone |
|---|---|
| Route classification; the `room`/`monsters`/`npc`/`item` resolvers | M8 |
| Fact-level dedupe within a conversation | M8 |
| Trade, gifts, disposition *changing* | M9 |
| Room and item canon written by discovery | M10 |
| Summarising or compacting old episodic memory | — |

M7 delivers **one** route's resolver early — `self`, the NPC's own canon —
because it is the only one that needs no classification to be useful, and
because without it the milestone has no observable gate.

---

## Already done — do not rebuild

- `nodes` with a JSON `data` column, `upsert_node`, and (after M6) an
  NPC row created at world creation rather than on first contact.
- `memories` + `memory_subjects` + `memory_vectors`, and `Store.recall()`'s
  graph-anchored hybrid path. **Episodic memory is not changing in M7.**
- `Theme.npcs` → `Npc(anchor, name, role, voice)`, and `_personas()` keyed by
  `anchor` == `Actor.id`.
- `MemoryWriter` draining on `RunEnded`, which C5 depends on for ordering.
- M6's `world.schema_version` and the refuse-on-mismatch path, which C1 turns
  into the first real migration.

---

## C1 — The `canon` table

**Files:** [memory/store.py](../../src/simulacra/memory/store.py)

```sql
CREATE TABLE IF NOT EXISTS canon (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id     TEXT NOT NULL,
    text        TEXT NOT NULL,
    provenance  TEXT NOT NULL,          -- authored | derived | told
    source_run  INTEGER,
    confidence  REAL DEFAULT 1.0,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_canon_node ON canon(node_id, status);
```

Surface, deliberately narrow:

- `Store.add_canon(node_id, text, provenance, *, source_run=None, confidence=1.0)`
- `Store.canon(node_id, *, provenance=None, limit=None)` → active rows only,
  `authored` first then `derived` then `told` (see C6 for why the order is
  load-bearing).
- `Store.retire_canon(node_id, *, provenance=None)` → sets `status='retired'`.

**`provenance` is validated on write.** A closed set of three, rejected with a
`ValueError` rather than stored — the whole design rests on this column meaning
what it says, and a typo that silently writes `"derived "` produces a row that is
recall-eligible and invisible to `retire_canon`. `observed` and `generated` are
**not** valid values here: `observed` lives in `memories`, and `generated` is
never persisted anywhere. Say so in the docstring, because the natural mistake is
to add them for symmetry.

**Bump `SCHEMA_VERSION` to 2 and write the migration.** This is additive —
`CREATE TABLE IF NOT EXISTS` plus an `UPDATE world SET schema_version=2` — so it
is the easiest migration this project will ever have, which makes it the right
one to establish the pattern on. M6's version check currently only refuses; give
it a forward path: a table of `{from_version: migration_fn}` applied in order,
and refuse only when there is no path.

**Tests:** an invalid provenance raises; `canon()` returns active rows in the
documented order and never a retired one; a v1 database opens, migrates to v2
and keeps its runs and memories intact.

---

## C2 — Authored canon, and a second NPC

**Files:** [world/theme.py](../../src/simulacra/world/theme.py),
[themes/simulacra.toml](../../themes/simulacra.toml)

Add `canon` (a list of short strings) and `depth` (int, default `1`) to the
roster entry, and to the frozen `Npc` dataclass:

```toml
[[npcs.roster]]
anchor = "npc:archivist"
name = "the Archivist"
role = "keeps count of everyone who has gone down"
voice = "Terse and clerical, like reading from a ledger..."
depth = 1
canon = [
  "Has kept the ledger since before the current numbering began.",
  "Will not go below the third floor, and does not explain why.",
]
```

**Add a second NPC to the roster.** Not decoration — with one NPC, every test of
"canon is anchored to the right entity" passes whether the anchoring works or
not, which is exactly the *demo passing for the wrong reason* failure M4 warned
about. A second NPC at a different `depth` is the cheapest way to make the
anchoring falsifiable, and M8's `npc` route (an NPC talking about another NPC)
has nothing to talk about without one.

**This surfaces two existing defects, both of which C3 has to absorb:**

- `_place_npcs` ([floorgen.py](../../src/simulacra/world/floorgen.py)) puts
  **every** roster NPC in the same shrine on `NPC_DEPTH`. Two NPCs currently
  means two NPCs in one room.
- `_talk` resolves with `next((a for a in npcs if needle in a.name.lower()),
  npcs[0])` — the `npcs[0]` fallback is correct only while a room can hold at
  most one NPC, which [conversation-system.md](../conversation-system.md)
  already flags as "fragile if that constraint is ever relaxed". Relaxing it is
  what this task does.

Replace the module-level `NPC_DEPTH` constant with the per-NPC `depth` field.
Which floor an NPC lives on is content, not structure, and it belongs in the
theme pack next to their voice. Keep the constant's reasoning in a comment on
the field's default — an NPC placed below the median run depth is one most runs
never meet, and an NPC nobody meets accumulates no canon.

**Tests:** authored canon loads into `Npc`; a roster with two NPCs at different
depths places them on different floors; `test_nothing_hostile_shares_the_npc_room`
still holds for both.

---

## C3 — NPCs as persistent entities

**Files:** [engine/state.py](../../src/simulacra/engine/state.py),
[world/floorgen.py](../../src/simulacra/world/floorgen.py),
[engine/loop.py](../../src/simulacra/engine/loop.py)

At **world creation** (M6's `create_world` path), upsert one `nodes` row per
roster NPC and seed its `authored` canon. `data` carries the state that has to
outlive a run:

```json
{"home_depth": 1, "room_id": "d1r4", "disposition": 0, "inventory": [],
 "last_canon_run": null}
```

Then invert the placement dependency: **`_place_npcs` reads the node, it does not
decide.** An NPC with a stored `room_id` is placed there; one without gets
assigned a room and the assignment is written back. That single change is what
makes M9's NPC movement a state update rather than a floorgen rewrite, and it is
the reason C3 belongs here rather than with the system that needs it.

Fix `_talk`'s resolution while the constraint is being relaxed: match the
addressee against roster names and require a match when the room holds more than
one NPC, rather than falling through to `npcs[0]`. Ambiguity should ask ("Which
of them?"), not guess — guessing wrong here anchors the *wrong* NPC's canon and
the failure is silent.

**Tests:** an NPC's node row exists before it has ever been spoken to; a
placed NPC returns to the same room on a second run; two NPCs in one room and an
unaddressed `talk` produces a disambiguation notice and no model call.

---

## C4 — `told`: the player asserts something

**Files:** [engine/loop.py](../../src/simulacra/engine/loop.py) (`_talk`),
[engine/events.py](../../src/simulacra/engine/events.py)

The narrow half of "NPCs learn from what players say", and the half that is safe
without route classification: when the player's line is an **assertion** rather
than a question, record it as `told` canon anchored to that NPC.

Detection stays tier 0 in M7 — the `tell` verb, which the parser does not have
yet and should get (`tell`, `say`, `inform` → a `tell` verb with an addressee and
a claim). A tier-1 classifier for "was that a statement or a question" is M8's
route work; do not build it here.

**What makes this safe** is the same thing that makes it useful. The row is
`provenance='told'`, `confidence` below 1.0, and it is *never* promoted to
`derived` or `observed` by any code path. The NPC repeats it as hearsay because
that is what the prompt says it is (C6). A lying player produces an NPC that
believes something false, which is content.

**Cap it.** Retain at most N `told` rows per NPC, retiring oldest-first — an
unbounded channel the player writes into directly is a prompt-bloat vector and a
grief vector, and N is code's to own.

**Tests:** `tell the archivist the arm was still moving` writes exactly one
`told` row anchored to that NPC with confidence < 1.0; the N+1th retires the
oldest; nothing the *NPC* says is ever written.

---

## C5 — Derived canon: the refresh

**Files:** new `memory/canonist.py`,
[\_\_main\_\_.py](../../src/simulacra/__main__.py)

One tier-3 structured call per NPC per refresh. Input: that NPC's `authored`
canon plus its episodic memories. Output: one to three short, stable facts,
written as `derived`.

```python
CANON_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {"type": "array", "maxItems": 3,
                  "items": {"type": "string"}},
    },
    "required": ["facts"],
}
```

**The depth-1 rule is enforced in the query, not in a comment.** The refresh
selects `provenance = 'authored'` and rows from `memories` — there is no code
path that hands it a `derived` row, and no code path that hands it anything the
narrator generated. Write the test that asserts the prompt contains no `derived`
text before writing the call itself; it is the single guard that keeps canon from
compounding on its own output across a world's lifetime.

**Run it at run end, not at startup.** `Session.close()` already runs
unconditionally while the player is reading an epitaph — that is dead time this
milestone can spend for free, and refreshing *after* a run means canon grows by
one increment per run, which is what "progressively created" means. Three
ordering requirements:

1. **After `MemoryWriter.close()`**, which drains and embeds this run's
   memories. Refreshing first reads a world one run out of date.
2. **Before `store.end_run()`**, so `source_run` is the run that produced it.
3. **Skip an NPC whose episodic memory has not grown** since `last_canon_run`.
   Without this, every exit burns 25 s regenerating identical canon.

**It must never hang the exit.** A hard timeout, failure swallowed the way
`epitaph` already is (`_die` treats a failed epitaph as an empty string, not an
error), and `--no-canon` to skip it outright. A player closing the game must
close the game.

**Cap the total.** Retire the oldest `derived` rows beyond M per NPC. Canon that
only ever grows becomes a prompt-bloat problem some number of runs out, and the
cap is cheaper than the compaction pass it would otherwise need.

**Tests:** the refresh prompt contains authored canon and episodic text and no
`derived` text; an NPC with no new memories is skipped; a raised exception in the
call leaves `close()` completing normally; `source_run` is the ending run.

---

## C6 — Canon reaches the prompt

**Files:** [narrate/narrator.py](../../src/simulacra/narrate/narrator.py) (`npc`),
[engine/loop.py](../../src/simulacra/engine/loop.py) (`_talk`)

The `self` route's resolver, arriving early. `Narrator.npc()` gains a `canon`
argument, kept **separate** from `recollections` — the model must be able to tell
"what I am" from "what I saw happen to someone else", and merging them is how
M4's prompt came to instruct an NPC to recite a death regardless of question.

Order in the prompt is load-bearing and follows what M4 measured: a small model
attends hardest to the end. Persona and canon first as standing context; episodic
recollections last, where the existing death-sorting already puts the most
important thing.

```
You are the Archivist. <role> <voice>
What is true of you:
- <authored>
- <derived>
A delver once told you:                  ← only when told rows exist
- <told>          (say this is hearsay if you repeat it)
You remember, from earlier delvers:      ← unchanged
- <recollection>
```

**Keep the additions terse and closed.** This is new prompt surface for a model
that has twice degenerated on loose instructions (the echo, and the
"1234567890" repetition collapse). The hearsay rider is one clause, not a
paragraph, and the existing `looks_like_echo` / `looks_degenerate` guards apply
to `npc()` unchanged — check they still fire against the longer prompt rather
than assuming it.

**Do not touch the recollection instruction.** "Say out loud what happened to the
delver you remember" stays exactly as it is in M7. Softening it is M8's job, and
it is only safe once routes can tell the model *why* a recollection was
retrieved.

**Token budget.** Persona + canon + told + recollections against a ~600-token
prompt. Count it with the caps from C4 and C5 at their maximum, not at their
typical — the failure arrives on the world where every NPC is full.

**Tests:** assert on the prompt, per M4's rule. Canon appears; canon and
recollections are in separate labelled sections; a `told` row carries the hearsay
rider; an NPC with canon but no recollections does not get the
"you remember nothing" branch (which today would be its only state).

---

## C7 — `--forget` learns about canon

**Files:** [\_\_main\_\_.py](../../src/simulacra/__main__.py),
[memory/store.py](../../src/simulacra/memory/store.py)

M6 shipped `--forget <npc>` against episodic memory with a docstring saying M7
extends it. Extend it: retire `derived` and `told` canon, keep `authored`, leave
the node row and its `authored` seed intact so the NPC comes back as itself
rather than as nothing. Reset `last_canon_run` so the next run rebuilds.

**Tests:** after `--forget`, `authored` canon survives and `derived`/`told` do
not; the NPC still has a node row and a home; a second NPC is untouched.

---

## C8 — Measure

**Files:** a note appended to [plan.md §9](../plan/plan.md#9-milestones).

Three numbers, in the manner of M2's latency table:

| | expected |
|---|---|
| canon refresh, per NPC | ~25–30 s, tier 3, at exit only |
| refreshes skipped (no new memories) | most exits after the first few runs |
| dialogue prompt, worst case | under the ~600-token budget with C4/C5 caps full |

And one qualitative check that cannot be automated: **read five consecutive
runs' derived canon for one NPC.** If run 5's canon is a paraphrase of run 1's,
the depth-1 rule is holding but the refresh is learning nothing, and the input
selection needs work before M8 builds on it. If it has drifted somewhere the
authored canon does not support, the rule is *not* holding and the query is
wrong. Both failures look like "plausible canon" from a single sample.

---

## Order and dependencies

C1 → C2 → C3 is the chain that has to land first: the table, then the content,
then the entity that owns it. C4 and C6 both need C1 and C3. C5 needs C1 and C2
(it reads authored canon). C7 needs C1. C8 is last.

Suggested order: **C1, C2, C3, C6, C4, C5, C7, C8** — C6 before C4 and C5 so
authored canon reaches the prompt as early as possible and the milestone has
something playable to judge the rest against. Getting canon into a live NPC's
mouth on day two is worth more than getting it there complete.

## Definition of done

- [ ] An NPC says something true about itself that was never a `Transcript`
- [ ] A second NPC exists, lives on a different floor, and has its own canon —
      and no query returns one NPC's canon anchored to the other
- [ ] `derived` canon is generated from `authored` + `observed` only, asserted
      by a test on the prompt
- [ ] Told-to canon is repeated as hearsay and never promoted
- [ ] Nothing the NPC says is written anywhere durable
- [ ] The refresh runs at exit, is skipped when nothing changed, and cannot hang
      or crash the exit
- [ ] A v1 world opens, migrates to v2, and keeps its runs and memories
- [ ] `--forget` leaves an NPC with its authored persona and nothing else
- [ ] Dialogue prompt stays inside the tier-2 budget with every cap full

## Watch for

**The demo passing for the wrong reason** — M4's own warning, and the reason C2
adds a second NPC. A single NPC in a single-NPC world produces convincing canon
whether or not `node_id` anchoring works at all, because everything in the table
belongs to it. Assert on the anchor, not on the output reading well.

**Provenance rot.** The design's entire safety property is one string column
meaning what it says. The ways it degrades are all quiet: a code path that writes
`derived` from a `derived` row, an `observed` value added "for symmetry", a
retired row that a query forgets to filter. The `provenance` validation in C1 and
the prompt-contents test in C5 are the two places this is actually enforced —
treat both as load-bearing tests rather than coverage.

**Canon that is not stable is not canon.** If the refresh rewrites an NPC's
backstory every run, the player is talking to a different character each time and
the milestone has failed even with every test green. That is what C8's
five-run read is for. The lever if it drifts is narrowing the refresh's input
selection, not raising the temperature or the token budget.

**Exit-time work is invisible until it breaks.** A refresh that hangs, a
`--stats` block that never prints, a `RunEnded` that leaves the process alive for
30 s — none of these show up in a test that calls `Session.close()` with a fake
client. Play it, close it, and time the close.

**Two NPCs in one room was never exercised end to end.** C3 relaxes a constraint
that has held since M4 by accident rather than by design. The disambiguation path
is new code on a verb that already has a documented resolution weakness; give it
its own test rather than assuming the C3 change covered it.
