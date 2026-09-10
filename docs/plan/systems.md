# Simulacra — systems plan (M6+)

The POC is complete and playable. This document covers what comes after it: a
persistent world, NPCs with knowledge worth having, and interaction that changes
things.

It is a companion to [plan.md](plan.md), not a replacement. Everything in §1
(the hardware constraint) and §2 (the latency budget) of that document still
governs — **a new system states its tier before it gets built**, and nothing
here is exempt.

---

## 0. What this supersedes

[conversation-improvements-tasks.md](../tasks/conversation-improvements-tasks.md)
scoped three fixes to the conversation system. T1 (query-aware recall) shipped in
`2893530` and stays. **T2 and T3 are cancelled**, not deferred:

- **T2** ("I don't know" via a recall-distance threshold) is deleted by §6.
  A retrieval route that resolves to nothing is an honest "I don't know" with no
  threshold to calibrate. T2's own task doc flags that the cutoff "can only be
  exercised with fake, chosen-for-the-test distances" and needs live tuning —
  that is work this design removes rather than performs.
- **T3** (in-conversation scratch buffer) is a hand-rolled degenerate case of
  §7. Building it standalone means building it twice, and the raw-text window it
  proposes is strictly weaker than fact-level dedupe.

The review that produced this document is in §1.

---

## 1. Why the conversation work stalled

Three structural findings, each verifiable in the current tree.

**The parser has one target slot, and conversation needs two.**
[`Intent`](../../src/simulacra/engine/parser.py) is `verb` + `target`, so
`ask archivist about the arm` reaches `_talk()` as the single string
`"archivist about the arm"` — addressee and subject fused.
[`_topic_of()`](../../src/simulacra/engine/loop.py) splits them back apart with
three ordered head-only stopword passes, and `2893530`'s commit message spends
three paragraphs on why the pass order is what it is. That is an ad-hoc parser
accreting inside a verb handler. It needs a fourth pass for `tell him about the
corpse`, a fifth for `show her the ledger`, a sixth for `give him the arm` — and
trade and gifts (§9) need all three of those. The fix is a slot in the intent,
not more passes.

**There is exactly one class of content in the memory system.** Only two sites
emit `Transcript` ([loop.py](../../src/simulacra/engine/loop.py), in `_talk` and
`_die`), and the first deliberately records the *shape of the question* and never
the answer. Every content-bearing memory in the database is therefore
`"A delver was killed by X on floor N"`, worded by code. T1, T2 and T3 are all
improvements to retrieval and phrasing over a single fact type. No retrieval work
fixes *there is nothing else to retrieve*. **Conversation quality was being
attacked at the prompt layer when it is a world-authoring problem.**

**The graph is written and never read.** `persist_floor` and `_record_death`
write nodes and typed edges faithfully. `Store.neighbors()` is called by tests
only — no engine code traverses an edge. `Store.recall(about=...)` filters on
`memory_subjects`, a one-hop tag join, not the graph. So "vector search and graph
db for efficient prompts" is currently vector search plus a subject tag, and
`chat_with_tools` ([client.py](../../src/simulacra/llm/client.py)) has zero
callers.

### The root cause behind all three

M4 found that storing an NPC's reply caused it to quote itself back on later
runs, crowding out real memories ([M4-tasks.md](../tasks/M4-tasks.md), Finding
2). That was diagnosed as *don't store replies*, and the fix was to store nothing.

The actual defect was **no provenance**: generated speech entered the same
undifferentiated pool as observed fact and was then recalled back into a
generation prompt. Killing the loop by starving the system is why the Archivist
has nothing to say, and it is why the T1/T2/T3 task doc had to put "let NPCs
learn from what players say" explicitly out of scope. §5 fixes the loop
structurally instead, which is what unblocks everything else.

---

## 2. The systems, at a glance

| | System | Owns | Tier | Unblocks |
|---|---|---|---|---|
| **S0** | [Persistent world](#4-s0--the-persistent-world) | world seed, schema version, reset | 0 | everything below |
| **S1** | [Canon](#5-s1--canon) | typed knowledge + provenance | 3, amortised | backstory, hearsay, room lore |
| **S2** | [Retrieval routes](#6-s2--retrieval-routes) | what a prompt gets to contain | 0–1 | topics, small prompts, "I don't know" |
| **S3** | [Conversation session](#7-s3--conversation-session) | state within one conversation | 0 | non-repetition |
| **S4** | [Action vocabulary](#8-s4--action-vocabulary) | anything that changes the world | 1 | trade, gifts, NPC movement |
| **S5** | [Lazy expansion](#9-s5--lazy-world-expansion) | room contents that don't exist yet | 2 | progressive discovery |

The dependency chain is real, not aesthetic: S1 needs S0's persistent entity
rows; S2 retrieves from S1; S3 dedupes what S2 surfaces; S4 mutates what S1
stores; S5 writes S1 canon on discovery.

---

## 3. Decisions

Recorded so they can be revisited deliberately rather than drifted away from,
in the manner of [plan.md §3](plan.md#3-decisions).

| Decision | Choice | Why |
|---|---|---|
| Floors across runs | **Persist** | §4 — makes the graph the actual world; "you find your own corpse" |
| Floor storage | **Recompute layout, store identity** | §4 — layout is microseconds from a seed; concepts and canon are not |
| Reset | **Archive, never delete** | §4 — a persistent world is expensive to lose; destructive defaults are wrong |
| Knowledge model | **Typed, with provenance** | §5 — the M4 loop becomes impossible instead of avoided |
| Derived canon depth | **Exactly 1, forever** | §5 — generated-from-generated is unbounded drift |
| Topic handling | **Named routes, not one embedding** | §6 — you do not semantically search for what the engine already knows |
| Route classification | **Two-stage, like the parser** | §6 — keeps the common case at tier 0 |
| "Creative tool calling" | **Constrained structured output** | §8 — same architecture, better reliability on a 1.7b |
| Discovery budget | **Code owns it, model owns content** | §9 — same split as floorgen/director |

---

## 4. S0 — The persistent world

**Tier 0.** No model calls. Small, and it goes first because every system below
writes durable state and a schema change is cheaper now than after another
3 MB of canon accumulates.

### What has to change

Floors do not currently persist. `new_run` generates floor 1 from
`random.randrange(1 << 30)` unless `--seed` is passed, so every run is a
different dungeon. The single `seed` is also doing two jobs at once — floor
layout, and (via `seed ^ 0x5EED`) in-game dice.

Split it:

- **World seed** — a property of the database, written once at world creation.
  Decides layout at every depth, via one helper both call sites go through:
  `floor_rng(world_seed, depth)`. It seeds from `f"{world_seed}:{depth}"`
  rather than `world_seed ^ depth`, so adjacent depths do not differ by a
  single bit.
- **Run seed** — per-run, recorded in `runs.seed`. Decides dice only. Two runs
  through the same world play differently but walk the same floors.

A new single-row table holds the world's identity:

```sql
CREATE TABLE IF NOT EXISTS world (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    world_seed     INTEGER NOT NULL,
    theme          TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    created_at     REAL NOT NULL
);
```

`schema_version` is not optional. A world the player has invested twenty runs in
must be able to *detect* an incompatible build and say so, rather than crash
against a changed table.

### Layout is recomputed; identity is stored

Do **not** serialise the floor graph. `floorgen` is deterministic and runs in
microseconds — regenerating floor 7 from `world_seed ^ 7` is cheaper than
reading it back. What cannot be regenerated is everything the model contributed:
floor name, goal, motifs, per-room concepts, and (later) S1 canon and S5
discoveries. Those are stored and re-attached after generation.

This makes the existing `persist_floor` writes load-bearing rather than
redundant, and it means the director's ~19 s tier-3 call is paid **once per
floor for the life of the world**, not once per run. That is a substantial
change to the latency profile in the player's favour and should be measured as
such.

### Reset

Three operations, deliberately different in scope:

| Command | Effect |
|---|---|
| `--new-world` | Archive `saves/world.db` to `saves/world-<timestamp>.db`, create a fresh world with a new seed |
| `--forget <npc>` | Retire that NPC's derived canon and episodic memories; keep authored canon |
| `--db <path>` | Already exists — the multi-world mechanism, unchanged |

**Archive, never delete.** The entire premise of a persistent world is that
losing it costs something. A destructive default on a file the player has spent
twenty runs filling is the wrong default, and disk is not the constrained
resource on this box.

`--forget` is the escape hatch S1 makes necessary: derived canon is generated,
and a persistent world means a bad generation is permanent unless something can
retire it. Retiring one NPC is a far better answer than discarding the world.

### Migration

The current `saves/world.db` is 3.3 MB of POC-era play against a schema this
milestone changes. Archive it as a test fixture rather than migrate it — there is
nothing in it worth carrying, and a real migration path is worth writing for the
first schema change that happens to a world someone cares about, not this one.

---

## 5. S1 — Canon

**Tier 3, amortised to near-zero.** One call per NPC per canon refresh, reused by
every conversation in every run thereafter.

Two things are currently conflated in the `memories` table and want separating:

- **Episodic memory** — things that happened. Run-scoped, timestamped, ranked by
  relevance. *"A delver died on floor 3."* This is what `memories` already is,
  and it stays.
- **Canon** — stable properties of an entity. *"The Archivist's brother went
  into the lower stacks and did not come back."* Not an event; not run-scoped;
  authored once and amended, never re-derived per turn.

They have different lifecycles, different write paths and different retrieval
patterns, so they get different tables.

```sql
CREATE TABLE IF NOT EXISTS canon (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id     TEXT NOT NULL,          -- whose: npc:archivist, room:d1r3, item:...
    text        TEXT NOT NULL,
    provenance  TEXT NOT NULL,          -- authored | derived | told
    source_run  INTEGER,
    confidence  REAL DEFAULT 1.0,
    status      TEXT NOT NULL DEFAULT 'active',   -- active | retired
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_canon_node ON canon(node_id, status);
```

### The provenance vocabulary

This is the keystone of the whole design. Five values, and the rule that makes
M4's bug structurally impossible:

| Provenance | Source | Recall-eligible | Notes |
|---|---|---|---|
| `authored` | theme pack | yes | the seed persona; never mutated |
| `derived` | tier-3 generation from `authored` + `observed` | yes | the "progressively created" part |
| `told` | the player asserted it in conversation | yes, **as hearsay** | may be false — that is a feature |
| `observed` | `Transcript` → `memories` | yes | episodic, stays in the existing table |
| `generated` | streamed room prose, NPC replies | **never** | not persisted as knowledge at all |

> **The rule:** `derived` canon is generated from `authored` and `observed`
> only — never from other `derived` canon, and never from `generated` prose.

That single line does two jobs. It closes the M4 loop (generated speech can no
longer re-enter as fact), and it caps generation depth at exactly **1**, so
canon cannot drift by compounding on its own output across twenty runs. Model
output is a leaf, always.

`told` is what finally lets an NPC learn from the player, which the previous task
doc had to rule out of scope. It is safe here precisely because it is typed: an
NPC repeating a player's claim is repeating *hearsay it knows is hearsay*, and a
lying player is content rather than corruption.

### Amendment, not deletion

`status='retired'` supersedes a fact without losing it. In a world that persists
across dozens of runs, the history of what an NPC used to believe is worth more
than the row it costs, and `--forget` (§4) needs something to set.

### The persistent NPC entity

NPCs get a `nodes` row today only after being spoken to
([loop.py](../../src/simulacra/engine/loop.py), `_talk`). They should be upserted
at **world creation** from the theme roster, with `data` carrying location,
disposition and inventory. That row is the home for everything §8 needs — an NPC
cannot be given a gift, cannot move, and cannot hold an opinion of the player
until it is an entity rather than a frozen dataclass in a TOML roster.

---

## 6. S2 — Retrieval routes

**Tier 0, escalating to tier 1.** The efficiency system, and the one that
deletes T2.

The current design has one retrieval channel: embed a string, KNN it against
memories tagged with this NPC. So *"what's down that corridor"* and *"how did
the last delver die"* go through identical machinery, and the answer to the
first is a vector search over deaths.

Replace it with **named routes**. The player's topic classifies into a route;
each route has its own resolver; only one of them touches the vector index.

| Route | Resolver | Vector search |
|---|---|---|
| `self` | the NPC's own canon (`authored` + `derived`) | no |
| `room` | live room census + `room:` canon | no |
| `monsters` | graph: hostile actors here and one hop out | no |
| `npc` | graph: other NPC nodes in `met_npcs` + their canon | no |
| `item` | graph: items in room and inventory + item canon | no |
| `past` | **episodic recall — today's `_recall_for`, unchanged** | **yes** |
| `trade` | §8 | no |
| `unknown` | nothing | no |

**Six of eight routes never embed anything.** You do not run a semantic search to
answer "what monsters are nearby" — the engine already knows, exactly, for free.
Vector search returns to the one job it is genuinely good at: unstructured recall
over prior runs. Prompts shrink because each one carries only what its own route
resolved, instead of three memories selected by topical proximity to a sentence.

And `unknown` — or any route that resolves to an empty set — *is* the honest
"I don't know." No distance threshold, no live calibration, no `via == "both"`
guard against an embedding outage reading as maximal relevance. The branch T2
was going to spend a milestone tuning becomes a truth about the data.

**M8 found that the refusal cannot be a prompt at all.** Told "say you do not
know", a 1.7b invents an answer — even with every other fact stripped out of the
prompt, it invents from the question. So a route that resolves to nothing is
spoken verbatim from the theme pack, with no model call: §7's *the LLM proposes,
code disposes*, arriving in conversation. Saying nothing is now free, which is
the right price for it.

### Classification is two-stage, like the parser

Route classification must not put a blocking tier-1 call on every conversation
turn. Use the shape [parser.py](../../src/simulacra/engine/parser.py) already
proves: a deterministic keyword table first (`"door" "corridor" "exit"` → `room`;
`"you" "yourself" "your"` → `self`), escalating to a ~32-token structured call
only on a miss.

This is viable *because* of §1's first finding: once the intent carries a clean
topic slot instead of a fused address-plus-topic string, a keyword table has
something reliable to match against. The parser fix and the route table are the
same piece of work seen from two ends.

**Cost to measure:** talk turns are tier 2 today. Worst case they become tier 1 +
tier 2. The stage-1 table is what keeps that off the common path, and its hit
rate is the number that decides whether this design holds — measure it the way
prefetch hit rate was measured in M2.

---

## 7. S3 — Conversation session

**Tier 0.** Pure engine state.

```python
@dataclass
class Conversation:
    npc_id: str
    exchanges: list[tuple[str, str]]   # (asked, said), last 2
    surfaced: set[str]                 # fact ids already spoken aloud
    open_route: str | None
```

The important field is `surfaced`, and it is why this is not T3. T3 proposed a
window of raw text so the model could see what it had already said. This tracks
**which facts** have been spoken, by id. When a route resolves entirely to facts
already in `surfaced`, the NPC says *"I've told you what I know about that"* and
does not restate — deterministically, in code, without asking a 1.7b model to
notice it is repeating itself.

That is the actual fix for the transcript in
[conversation-system.md](../conversation-system.md): eight questions, one fact,
eight phrasings. Routes make the eight questions retrieve different things;
`surfaced` makes the one fact get said once.

T3's constraint is kept exactly: this lives in engine state, **never touches
`Store`**, is never embedded, and dies with the run. A same-run in-memory scratch
buffer cannot reproduce the M4 bug because it never becomes persistent knowledge.

---

## 8. S4 — Action vocabulary

**Tier 1.** One structured call, the pattern
[judge.py](../../src/simulacra/engine/judge.py) already validates.

Trade, gifts and NPC movement are all the same shape: something proposes a change
to the world, and code decides whether it happens. That is §7 of plan.md — *the
LLM proposes, code disposes* — generalised from improvised player actions to
cover NPC decisions.

**Amended by M9, which built it.** Two thirds of the original claim held. Monster
behaviour does *not* join them: routing it through a tier-1 call would put one on
every combat turn, and `combat.py` is dice — seedable and testable without a
model. And one schema does not fit both callers, because the judge resolves a
*physical* action with dice while a gift is a *social* choice with none. What
generalises is the architecture — a closed enum, code validating against state
the model does not get to assert, code clamping the result — so `dealings.py` is
its second instance rather than its second copy.

```json
{"action": "give|accept|refuse|follow|lead|warn|move|attack|nothing",
 "object": "...", "reason": "..."}
```

Code then validates against state the model does not get to assert: does the NPC
actually hold that object, is that room adjacent, is disposition sufficient for
that request. Then it applies, with the same hard clamps `judge.py` uses.

**Disposition** is a scalar on the NPC node (§5) moved by gifts, trade and
threats, and it gates which routes and actions are available at all. It is the
cheapest possible way to make trade *matter* rather than be a transaction — a
gift buys access to a route, not an item.

### On "creative tool calling"

notes.md asked for tool calling, and
[`chat_with_tools`](../../src/simulacra/llm/client.py) has been sitting built and
unused since M0. **The recommendation is to implement this as constrained
structured output instead**, and the reasoning is measured rather than
aesthetic:

- Ollama's `format` grammar *guarantees* a well-formed response matching the
  schema. Failures become semantic, not syntactic — which is what
  `client.structured()`'s docstring already observes.
- M3 measured a 1.7b model contradicting its own stated reason in **3 of 5**
  live samples on a six-way enum, fixed to **5 of 5** by splitting it into two
  simpler choices. A tool-call decision is the harder version of that problem,
  not the easier one.
- The client's own docstring for `chat_with_tools` says a tool decision "costs
  about what a structured call costs but is far less reliable."

The architecture notes.md wanted — the model choosing an action from a
vocabulary of world-affecting operations — is delivered in full. Only the wire
format differs, in favour of the one this hardware is reliable at. Keep
`chat_with_tools` for a batch-time experiment: §5's derived-canon generation is
tier 3 and could afford both tool calling and `think: true`, which
[plan.md §9](plan.md#9-milestones) already reserves for exactly that kind of
offline work.

---

## 9. S5 — Lazy world expansion

**Tier 2, streamed.** The last system, because it depends on all the others.

`look at <thing>` currently either finds a known entity or fails. Progressive
discovery makes an unknown target a *question about the world* instead: is this
a plausible thing to find here, and if so, what is it?

```
look at the shelves
  │
  ├─ known entity in room/inventory?      → describe it (today's behaviour)
  │
  ├─ found here before?                    → replay it, no model call
  │
  ├─ a noun the room's own prose used?     ← always findable. M10 built it here
  │                                          rather than from a slot list: the
  │                                          director had been naming things
  │                                          since M2 and the engine denied them
  │
  ├─ else a fixture for this room kind?    ← theme pool, RNG per *room*
  │
  ├─ tier-2 call describes it              ← model owns the content
  │
  └─ written as room canon, provenance `derived`
```

Two properties make this a system rather than a random-content generator:

**Discovery is idempotent.** Looking twice gives the same answer, because the
first look wrote canon. With floors persisting (§4), it gives the same answer
*three runs later*, which is the entire point — a world that rewards
re-exploration has to remember what it showed you.

**Discovery has a budget.** A room is exhausted after N discoveries. Code owns
N; the model owns what is behind it. Without this, a player grinds infinite
content out of one room and the dungeon stops meaning anything. Same split as
floorgen/director throughout: **structure is code's, identity is the model's.**

---

## 10. Milestones

| | Milestone | Contents | Gate |
|---|---|---|---|
| **M6** ✅ | Persistent world + reset | S0 — world table, seed split, recompute-and-reattach, `--new-world` / `--forget` | **Met.** Run 2 reached floor 3 with 0 model calls and 0.0 s of wall time |
| **M7** ✅ | Canon | S1 — `canon` table, provenance, persistent NPC entities, derived-canon refresh | **Met.** The Archivist now counts its dead, grown from deaths rather than authored |
| **M8** ✅ | Routes + session | S2 + S3, and the parser's topic slot | **Met.** Ten questions, ten answers, from five sources; 90–100% of routes cost nothing |
| **M9** ✅ | Action vocabulary | S4 — trade, gifts, NPC movement, disposition | **Met.** A gift unlocks the NPC's derived canon, live |
| **M10** ✅ | Lazy expansion | S5 — slots, budget, discovery-as-canon | **Met.** A noun the room's prose used is findable, and still there next run |

M8 is one milestone containing two systems on purpose. Routes without a session
still repeat facts; a session without routes has nothing worth deduping. They are
independently *testable* but not independently *valuable*, and shipping either
alone would read as no improvement.

Task decomposition follows the existing convention — one `docs/tasks/M<n>-*.md`
per milestone, written before implementation and amended with findings after.
[M6](../tasks/M6-persistent-world-tasks.md),
[M7](../tasks/M7-canon-tasks.md),
[M8](../tasks/M8-routes-tasks.md),
[M9](../tasks/M9-action-vocabulary-tasks.md) and
[M10](../tasks/M10-discovery-tasks.md) are **done, with findings**.
**The plan is complete.**

---

## 11. Risks

**Route classification cost.** Talk turns are tier 2 today; the worst case is
tier 1 + tier 2. The stage-1 keyword table is the entire mitigation and its hit
rate is the load-bearing number. If it lands below ~70%, the two-stage design is
not paying for itself and route classification should move into the existing
stage-2 parser call rather than being a second one.

**Derived canon drift.** Generating canon from canon compounds error across a
world's lifetime. The depth-1 rule (§5) is the mitigation and it must be enforced
in the query, not in a comment — the refresh call selects `provenance IN
('authored')` plus episodic memories, and there is no code path that feeds it a
`derived` row. Worth a test that asserts exactly that.

**Permanence cuts both ways.** A persistent world means a bad generation is
permanent. `--forget` and `--new-world` exist for this, and they are in M6 rather
than bolted on later specifically because M7 is the milestone that starts
producing content that might need retiring.

**Prompt growth across five systems.** Each of S1, S2, S4 adds prompt surface.
Individually small; collectively this is the thing most likely to quietly break
[plan.md §2](plan.md#2-latency-budget). Token-count check per milestone, against
the tier the milestone declared, not against the previous milestone.

**`told` canon is an attack surface on the fiction.** A player who tells an NPC
something absurd gets an NPC that repeats something absurd. This is intended —
hearsay is content — but the confidence field and the hearsay framing in the
prompt are what keep it from reading as the world being broken. If it reads badly
in play, the lever is prompt framing, not deleting the provenance.

**The 3.3 MB POC world is not migrated.** Deliberate (§4). If anything in it
turns out to be worth keeping, that decision gets more expensive after M6, not
less.
