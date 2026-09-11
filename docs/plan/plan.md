# Simulacra — plan

An endless text dungeon, generated and narrated by a local Ollama model, on a
machine that cannot afford to be generous about it.

Working title. The engine is setting-agnostic; theme lives in `themes/*.toml`.

---

## 1. The constraint that shapes everything

Measured on this box, not assumed:

| | tok/s | cold load | notes |
|---|---|---|---|
| `qwen3:1.7b`, think off | **~15** | 31 s | default chat model |
| `qwen3.5:2b`, think off | **~10** | 8 s | fallback if quality is short |
| `qwen3.5:2b`, think on | ~10 | — | burned all 200 tokens reasoning, **emitted no content** |
| `nomic-embed-text` | — | — | 274 MB, cheap, always resident |

`ollama ps` reports **96%/4% CPU/GPU**. The Quadro M1000M's 2 GB cannot hold even
the 1.7 b model, so this is a **CPU inference machine** — an 8-core Skylake mobile
i7 with ~10 GB free RAM. Three consequences drive the whole design:

**Thinking is off, everywhere in the turn loop.** It doesn't merely cost time, it
consumes the output budget and returns nothing. Reserved for offline experiments
only (§9).

**One resident chat model, forever.** A cold load costs longer than a typical
turn. The narrator, judge, director and parser fallback all share one model with
`keep_alive: 60m`. Model-switching mid-game is not on the table.

**Streaming is what makes it playable.** 15 tok/s is ~11 words/sec against a human
reading speed of ~4. Streamed prose *outruns the reader* and feels instantaneous.
The same 15 tok/s spent on a blocking JSON call, with a blank screen, feels
broken. So the rule is:

> **Stream everything the player reads. Keep everything they wait on tiny.**

---

## 2. Latency budget

Every LLM call site is assigned a tier. This is the plan's central discipline —
a new feature must state its tier before it gets built.

| Tier | Cost | Player experience | Call sites |
|---|---|---|---|
| **0** | 0 ms | instant | movement, look, inventory, combat dice, parser stage 1 |
| **1** | ≤ 120 tok, blocking, ~2–4 s | brief pause | judge verdict, parser stage 2 |
| **2** | ~180 tok, **streamed + prefetched** | feels instant | room prose, NPC dialogue |
| **3** | ~400 tok, blocking, ~25–30 s | once per floor, behind a spinner | floor director |

Budgets live in [config.py](../../src/simulacra/config.py) as per-call-site
`ModelPolicy` objects, not scattered through call sites.

**The prefetcher is the single biggest win.** On entering a room we know the only
places the player can go: its neighbours. A background worker narrates those
while the player reads the current room. By the time they type `north`, it's
cached. Two rules keep this from backfiring — one strictly serial worker (parallel
requests on CPU just make each other slower), and foreground preemption (a
prefetch that delays a real turn is worse than none). See
[prefetch.py](../../src/simulacra/narrate/prefetch.py).

Target: **90% of turns cost zero LLM time.**

---

## 3. Decisions

Settled during the design interview; the reasoning is recorded so it can be
revisited deliberately rather than drifted away from.

| Decision | Choice | Why |
|---|---|---|
| Storage | **SQLite only**, no Docker | §5 |
| Input | **Verb parser + LLM fallback** | most turns are tier 0 |
| Structure | **Roguelike runs, persistent world memory** | makes memory the point, not decoration |
| Generation | **Hybrid by scale** | §4 |
| Front end | **REPL now, TUI later** — behind an event seam | §6 |
| Resolution | **Dice for known verbs, LLM judge for improvisation** | §7 |
| Theme | **Configurable content packs** | §8 |
| POC | **Vertical slice including memory** | M4 |

---

## 4. Generation: hybrid by scale

Three layers, deliberately separated by cost:

**Code owns structure.** [floorgen.py](../../src/simulacra/world/floorgen.py) builds a
guaranteed-connected floor in microseconds: a spine from entrance to descent
built first, loops added only afterward so the floor is *winnable by
construction*. Difficulty scaling, loot placement and monster stats are code's
job. The model is never handed a layout problem, so it can never produce an
unreachable exit or an unwinnable floor.

**The model owns identity.** One tier-3 call per floor
([director.py](../../src/simulacra/world/director.py)) receives a room census and
returns a floor name, a goal, up to three motifs, and one short concept line per
notable room. It's told the last few floors' names so it doesn't repeat itself —
small models converge hard on a single idea otherwise. On failure the floor keeps
its procedural names and play continues, just flatter.

**The model owns prose.** Tier-2 streaming expands a concept line into two
sentences ([narrator.py](../../src/simulacra/narrate/narrator.py)). Cached in SQLite,
so revisiting a room is free.

The payoff: each floor has a distinct identity for one 25-second call, amortised
over the whole floor, while every individual room stays cheap.

---

## 5. Memory: one file, no daemons

notes.md — the founding brief, since removed from the tree and kept in git
history — called for ChromaDB + Neo4j in Docker. **Recommended against, and this
is the one place the plan departs from the notes.** A JVM with a 1–2 GB heap plus
a Chroma server would be competing for the same 10 GB and the same 8 cores as
CPU inference — the thing the whole design is organised around protecting.

[store.py](../../src/simulacra/memory/store.py) does both jobs in a single SQLite file,
**already built and verified working**:

- **Vectors** — `sqlite-vec` v0.1.9, real KNN over `float[768]`. Confirmed
  installed and functioning on this box's CPython 3.14, including
  `enable_load_extension`.
- **Graph** — a typed `edges` table. The queries this game needs are one and two
  hops ("what does this NPC remember", "who died on floor 4"), comfortably within
  what SQL joins do well. Cypher would buy expressiveness with no current use.

Cost: ~0 MB idle, one file to back up, delete or ship. The `Store` surface is
narrow enough that a Neo4j implementation could slot in behind it later if graph
queries ever genuinely outgrow SQL.

### Hybrid recall

`Store.recall()` combines both, and the combination is the point. Pure vector
search over a shared world returns whatever is *topically* closest — often
someone else's business. So we filter on the graph first, then rank
semantically:

```
recall(embedding=q, about="npc:archivist", exclude_run=current)
  -> edges/memory_subjects restrict to what this NPC actually witnessed
  -> sqlite-vec ranks those by relevance
  -> top 3, verbatim, into the dialogue prompt
```

Verified working: given three memories, two witnessed by the Warden and one not,
graph-anchored recall returns only the witnessed pair, distance-ranked.

**This is what makes runs cumulative.** Two or three quoted recollections cost a
handful of prompt tokens and buy an NPC who knows you died on floor four last
time. Continuity comes from targeted retrieval, not from a growing context
window — which is the only version of continuity this hardware can afford.

---

## 6. Event seam: REPL and TUI, one engine

The engine yields `Event` objects and never prints. A `Renderer` decides what
they look like. See [events.py](../../src/simulacra/engine/events.py).

```
for event in engine.turn(text):
    renderer.handle(event)
```

Prose arrives as `ProseStart` / `ProseDelta*` / `ProseEnd`, so a renderer can
paint token by token or just concatenate. Cheap certain events (`RoomEntered`,
`StatusChanged`) are emitted before expensive ones so a future TUI can update
panels while prose is still streaming behind them.

A Textual frontend is then a sibling of [repl.py](../../src/simulacra/ui/repl.py)
implementing the same protocol — not a rewrite. **Rule: if the engine ever needs
to know it's talking to a terminal, the seam has leaked.**

**M5 cashed this claim.** [tui.py](../../src/simulacra/ui/tui.py) is a second
renderer of the same stream, and building it cost the engine exactly one field:
`RoomEntered.via`, the direction travelled, which a grid layout genuinely cannot
derive from anything else it is told. Nothing else in `engine/` moved. The one
thing the seam did *not* cover was concurrency — the engine is a synchronous
generator and Textual is async — which the TUI absorbs on its own side by
running the turn in a worker thread and posting each event as it is yielded.

`Transcript` is the same trick applied to memory: the engine emits what's worth
remembering, and the memory layer observes. Memory is not a dependency of the
turn loop.

---

## 7. Resolution: the LLM proposes, code disposes

Known verbs resolve in [combat.py](../../src/simulacra/engine/combat.py) — d20, stats,
seedable, no I/O, fully testable without a model running. The balance curve is
code's responsibility and must stay verifiable in CI.

Improvised actions ("tip the brazier into the water", "bluff the ghoul") go to
[judge.py](../../src/simulacra/engine/judge.py), a tier-1 structured call returning a
verdict against a **closed enum** of effects — never free-form state:

```json
{"plausible": true, "difficulty": 14, "effect": "damage_target",
 "magnitude": 3, "reason": "steam scalds it"}
```

Code then rolls, **clamps magnitude against its own ceilings**, and applies. A
1.7 b model asked to freeform game state will invent effects, invent numbers, and
quietly rewrite the difficulty curve. The enum plus the clamp is what makes
improvisation fun without making the game unbalanceable. A failed judge call
returns *implausible* — never a free win.

---

## 8. Theme packs

The engine knows nothing about setting. [themes/simulacra.toml](../../themes/simulacra.toml)
carries prompt fragments (narrator voice, director brief, judge brief) and name
pools (rooms by `RoomKind`, monsters by tier, items by category, persistent NPC
roster with graph anchors). Swap with `SIMULACRA_THEME=<name>`.

Two details that matter more than they look:

- **Fragments stay short.** Every word is prompt tokens on every call, and prompt
  eval isn't free on CPU either. Two-sentence briefs; small models follow a tight
  brief far better than a long one.
- **A `banned` word list.** Small models reach for the same half-dozen adjectives
  relentlessly ("ancient", "eerie", "you feel a sense of"). Banning them
  explicitly is the cheapest quality win available.

Structure is code's, names are the theme's — which is why a new setting is a TOML
file, not a code change.

---

## 9. Milestones

**M0 — Ollama client** ✅ *done* · notes.md's "start with the ollama client"
[client.py](../../src/simulacra/llm/client.py): streaming, structured outputs, thinking
toggle, embeddings, tool calling, serialized requests, per-call-site policies,
built-in telemetry (`--stats`), and a `health()` preflight that names the exact
`ollama pull` needed.

**M1 — Walk around, no LLM** ✅ *done* · **[task breakdown →](../tasks/M1-tasks.md)**
Parser stage 1, run bootstrap, engine loop, REPL renderer, wiring, tests.
**214 tests green with the daemon unreachable**, and the M1 code path imports no
HTTP client at all. The event seam holds: `Engine._describe()` is the sole
producer of room text, enforced by a test.

**M2 — It reads like a game** ✅ *done* · **[task record →](../tasks/M2-tasks.md)**
Narrator with echo guard, prefetcher, director, theme wiring. **The latency
budget holds.** Measured over two live runs, three floors each:

| | measured | budget (§2) |
|---|---|---|
| tier 2, room prose | 5.4 s avg, 15.9 tok/s | streamed + prefetched |
| tier 3, floor director | 18.9 s avg | 25–30 s |
| **movement turns** | **0.00 s, every one** | tier 0 |

Prefetch hit rate is 70%, and the three misses per run are structurally
unavoidable — a new floor's entrance cannot be prefetched because the floor does
not exist until you descend. **Every prefetchable room hit.** The player waits
only at floor transitions, behind a `Thinking` beat.

**M3 — Consequence** ✅ *done* · **[task record →](../tasks/M3-tasks.md)**
Combat, judge, parser stage 2, `use`/`drink`, death and epitaph. Now it's a
roguelike. Runs end on floors 2–8 with a median of 4, and the balance simulation
guards that curve without a model. Two things needed fixing that the task doc did
not anticipate: the player had **no progression at all** (so "endless" capped out
at floor 4), and the judge's six-way effect enum made a 1.7 b model contradict
its own reasoning 3 times in 5 — split into two simple choices, now 5 of 5.

**M4 — Memory** ✅ *done — **the POC is complete*** · **[task record →](../tasks/M4-tasks.md)**
Transcripts written with embeddings off the turn loop, hybrid recall on NPC
encounter, and a persistent Archivist who remembers your last run. Verified live:
run 1 dies on floor 3, run 2 is told *"…was killed by a smudged figure on floor 3
after two turns."* Both models stay resident (`nomic-embed-text` 571 MB +
`qwen3:1.7b` 2.0 GB, no eviction), so M2's budget survives.

**M5 — Textual TUI** ✅ *done* · **[task record →](../tasks/M5-tui-tasks.md)**
A panelled frontend — prose log, status, fog-of-war map, input line — added as a
*second renderer*. **The seam held.** The engine's only change was the one the
task doc budgeted for: `RoomEntered.via`, the direction travelled, which a grid
layout cannot derive from anything else it is told. `test_seam.py` is unchanged.
The turn runs in a Textual worker thread and each event is posted as it is
yielded, so prose still streams token by token and an 18 s descent leaves the UI
responsive.

**The TUI is not competing with inference.** Measured on this box, frontend
process CPU (the game process only — inference is `ollama serve`):

| | idle, 20 s | while streaming, 72 s / 6 turns |
|---|---|---|
| REPL | 0.03 s cpu (**0.15%** of one core) | 0.13 s cpu (**0.18%**) |
| TUI | 0.04 s cpu (**0.20%** of one core) | 0.34 s cpu (**0.47%**) |

`doctor` run with the TUI open and idle on a real pty: **tier 2 = 15.9 tok/s**
against a **15.6 tok/s** baseline with nothing else running, tier 1 14.5 vs 14.3.
Both inside noise, and the TUI process burned 0.00 s of CPU across the benchmark
window. M2's budget is intact.

Two things earned their measurement. `ProseDelta` repaints are **coalesced on a
120 ms timer** — tokens arrive at ~15/s and repainting per token is 15 relayouts
a second for no gain over ~8. And the `Input` **cursor blink is disabled**: it was
the only thing in the UI repainting at idle, and it cost 5× the entire rest of
the frontend (1.00% → 0.20% of a core), stolen from the prefetcher narrating the
next three rooms.

**One bug found by playing it.** A status *panel* is not a status *line*: combat
emits no `StatusChanged`, so the hp bar sat at 23/23 for an entire fight and was
still reading 23/23 when the log said the killing blow landed. The REPL hides
this because a stale line has already scrolled away. Fixed without touching the
engine — `Damage.hp_left` is the player's hp and was already in the stream.

**POC closeout — bugfixes + one feature** 🚧 *in progress* ·
**[task breakdown →](../tasks/poc-closeout-tasks.md)**
Found by playing the finished POC, ahead of M6. TUI single-key movement was
submitting on the first keystroke of an empty line — before Enter, and before
the rest of the word — so anything starting with n/s/e/w (a "nudge", a
"search") turned into an unwanted move. The stage-2 parser fallback (48
tokens, `Settings.intent`) is intermittently misrouting creative attack
phrasing ("karate chop X") to `take` instead of `attack`/`improvise` — the
engine never actually loses track of a monster's hp, the verb is just
misfiled. And `look` has no way to ask about a specific item, NPC, or
monster; it can only re-describe the whole room.

The conversation system also needs work. Recall was topic-blind — it always
embedded a fixed "how did the delver die" query rather than the player's
actual question — which T1 fixes: an NPC now recalls against the topic the
player named, falling back to the death query only when they named none.
Still open: the prompt recites a recollection whether or not it answers the
question (no way to say "I don't know"), and every dialogue turn is
stateless, so an NPC can't tell it already said the same thing two replies
ago. Documented in [conversation-system.md](../conversation-system.md); fixes
were scoped in
**[conversation-improvements-tasks.md →](../tasks/conversation-improvements-tasks.md)**,
of which T1 shipped and **T2/T3 are superseded by [systems.md](systems.md)**.

**M6 — Persistent world** ✅ *done* · **[task record →](../tasks/M6-persistent-world-tasks.md)**
Floors stop being disposable. A `world` table owns the seed and a schema
version; layout is *recomputed* from `floor_rng(world_seed, depth)` while the
model's contribution — floor name, goal, motifs, room concepts — is stored in
the graph and re-attached. `--new-world` archives rather than deletes, and
`--forget <npc>` retires one NPC's memories without discarding the world.

**The payoff is larger than the milestone claimed.** Two runs through one world,
floor 1 to floor 3, on this box:

| | tier 3 (director) | tier 2 (prose) | wall to floor 3 |
|---|---|---|---|
| run 1, fresh world | 3 calls, 18.7 s avg | 10 calls, 4.9 s avg | **105.6 s** |
| run 2, same world | **0** | **0** | **0.0 s** |

Run 2 reached floor 3 having spent no model time at all. Stored identity removed
the director calls, and the prose cache key stabilised on the stored concepts,
which removed the narration too — so the player now waits once per floor for the
life of a world instead of once per floor per run.

Two bugs fixed on the way, both found by reviewing seed handling rather than by
playing. `descend()` generated every floor from `state.rng` — the *dice* stream —
so how many attacks you rolled on floor 1 decided the shape of floor 2, in direct
contradiction of the invariant `state.py` documents. And `Store.link()` has
offered `run_id=None` since M0 without it ever being insertable: `edges` is
`WITHOUT ROWID`, which makes primary-key columns implicitly `NOT NULL`.

**M7 — Canon** ✅ *done* · **[task record →](../tasks/M7-canon-tasks.md)**
NPCs stop having only deaths to talk about. A `canon` table carries typed
knowledge with provenance — `authored` from the theme pack, `derived` written up
at exit from authored canon plus episodic memory, `told` asserted by the player
and repeated as hearsay. `generated` prose is never persisted at all, which is
what closes M4's self-quotation loop structurally rather than by starvation, and
derived canon is grown only from authored and observed, which caps generation
depth at exactly 1 so canon cannot drift by compounding on itself.

NPCs became residents: seeded into the graph at world creation with a home,
a disposition and an inventory, placed from stored state rather than by
`floorgen`, and a second NPC (the Corrector, floor 3) makes canon anchoring
falsifiable for the first time.

**One bug no test could have found.** The narrator has had `looks_degenerate`
since M4, but it guards streamed prose — the canonist wrote structured output
straight into a table that outlives the run. The first live five-run read
produced *"The Archivist's ledger is currently marked with 123456789."* as
permanent canon. A bad line of narration scrolls away; a bad line of canon is in
every conversation that NPC ever has again.

**Input selection turned out to matter more than prompt wording.** Canon grown
from dialogue transcripts circled one noun and contradicted itself, because a
transcript records the shape of a question and not its answer. Preferring deaths
and discoveries, the same model started accumulating: *"entries for three
delvers who died on floor 1"* at run 3, *"six delvers killed on floor 1"* at run
5. It began counting, which is the Archivist's authored role.

**M8 — Routes + session** ✅ *done* · **[task record →](../tasks/M8-routes-tasks.md)**
Conversation stops having one retrieval channel. The player's topic classifies
into a named route — `self`, `room`, `monsters`, `npc`, `item`, `past` — and
each has its own resolver, of which **only `past` embeds anything**. You do not
run a 768-dimension nearest-neighbour search to find out what is in the room;
the engine already knows. Classification is two-stage like the parser, and
measured at **90–100% stage-1 hits** over ten varied questions, so the common
case stays tier 0.

A conversation session tracks which *facts* have been spoken, by key, so a
repeated question is answered "I have told you" in code rather than hoping a
1.7b notices. And the parser finally got a topic slot, which deleted
`_topic_of()`'s three ordered stopword passes from the talk handler.

**The finding that changed the design: a refusal cannot be a prompt.** Told "say
you do not know", `qwen3:1.7b` invents an answer — asked about the Corrector, a
person, it explained that the Corrector is "a device used to alter records".
Stripping every other fact out of the prompt did not help; it invented from the
question. So a route that resolves to nothing is now **spoken verbatim from the
theme pack with no model call**, which is §7's *the LLM proposes, code disposes*
arriving in conversation — and makes saying nothing free. Generalising: an
instruction whose whole purpose is to suppress output is the weakest possible
use of a small model.

Also fixed: the echo guard rejected dialogue that relayed its own prompt facts,
because it was written in M2 for a room census — data to *transform*, not
*relay*. Second milestone running where a guard built for one caller silently
failed a new one.

| | before (conversation-system.md) | after |
|---|---|---|
| eight questions | one fact, eight phrasings | ten questions, ten answers, five sources |
| turns that embed | every one | 3 of 10 |
| refusal / repeat turns | invented an answer | 0.0 s, no model call |

**M9 — Action vocabulary** ✅ *done* · **[task record →](../tasks/M9-action-vocabulary-tasks.md)**
NPCs stop only knowing things and start doing them. `give`, `ask X for Y` and
`follow` reach a closed decision enum in
[dealings.py](../../src/simulacra/engine/dealings.py); the model proposes and
code disposes, matching every named object against things that actually exist so
an NPC can never hand over something invented — permanently, in a world that
persists. Disposition is an int on the NPC's node, moved by a fixed step per
gift and never by a number the model supplied, and **a gift buys a route, not an
item**: `derived` canon unlocks at the warm band. Measured live, same question
either side of one gift:

```
[disposition 0] You have kept the ledger since before the current numbering began.
[disposition 1] The ledger is yours. ... You number both doors and delvers.
```

Every refusal disposition can decide costs **0.0 s** — no model call — which is
M8's finding applied one layer out. One tier-1 call per social decision, none
anywhere else.

**A latent bug this surfaced, live since M4.** `_talk` touches the NPC's node to
update `last_run`, and `upsert_node`'s `data=None` meant `{}` — so every
conversation turn erased that NPC's entire blob. Harmless while NPC nodes carried
nothing; from M7 it was quietly wiping `canon_memories` (so the canonist kept
re-deriving canon it had already written), and from M9 it erased a gift between
one turn and the next, which is how it was found. `data=None` now preserves.

That is the third of its shape in four milestones: a helper written when one
caller existed, whose default became wrong when a second arrived, failing
silently. M7's canonist had no output guard, M8's echo guard had the wrong rule
for dialogue, M9's node touch had the wrong default. None was caught by a test
written at the time; all three were caught by playing it.

**M10 — Lazy world expansion** ✅ *done* · **[task record →](../tasks/M10-discovery-tasks.md)**
The last one in [systems.md](systems.md), and the one that closes the arc M6
opened: a world cheap to re-walk needs a reason to re-walk it.

The narrator has been writing *"a cracked altar stands against the far wall"*
since M2, and `look at the altar` answered *"You don't see altar here."* The room
told the player what was in it and the engine disagreed. Now a noun the room's
own prose used is always findable, a fixture from the theme's per-kind pool is
findable if the room is fertile (rolled once per **room**, so retyping never
helps), and what turns up is written as canon on the `room:` node — the same
table M7 built for NPCs, used for the first time by something that is not one.
A second look replays it for free, and floors persisting since M6 means that
holds three runs later. A room is finished after three finds; code owns how many
and the model owns what.

Plausibility is graded in code, with no model call, which is what keeps `look`
free. A *discovery* does provoke — rummaging until you turn something up is not
eyeballing, and doing it with something hostile in the room should cost you.

**And a field declared in M1 that nothing had ever written to.** `Room.prose`
and `Room.described` have existed since the first milestone; the narrator's text
lived only in `prose_cache`, keyed by a hash. So grading against the prose was
grading against an empty string, and only nouns from the director's one-line
concept worked. `_describe` now records what it streamed.

That is the third of that shape in five milestones — `Store.neighbors` sat
unused until M8, `disposition` and `inventory` until M9, `Room.prose` until now.
Writing the slot early has repeatedly been the right call; the cost is that "it
exists" and "it works" drift apart quietly, and only playing it tells them
apart.

---

**The plan is complete.** M6–M10 are done, each with findings. Deferred within
it: thinking experiments (batch-time only), contradiction detection between
canon rows, and larger models if the hardware changes. `chat_with_tools` remains
built and unused on purpose — [systems.md §8](systems.md#8-s4--action-vocabulary),
amended by M9, argues constrained structured output delivers the same
architecture at the reliability this hardware actually has.
The POC's conversation work stalled for a structural reason, not a tuning one:
the memory system has exactly one class of content in it (deaths), the graph is
written and never read, and the parser's single `target` slot forces an ad-hoc
address/topic splitter into the talk handler. [systems.md](systems.md) reviews
that and lays out five systems — a persistent world, typed canon with
provenance, retrieval routes, conversation sessions, and one action vocabulary —
across M6–M10. **T2 and T3 below are cancelled by it**, not deferred: routes
delete T2's threshold-calibration problem outright, and T3 is a weaker version
of the session object.

Still deferred within that plan: thinking experiments (batch-time only, e.g.
derived-canon generation, where 30 s is free) and larger models if the hardware
changes. Tool calling stays built and unused on purpose — systems.md §8 argues
constrained structured output delivers the same architecture at the reliability
this hardware actually has.

---

**M11 — Playtest fixes** ✅ *done* · **[task record →](../tasks/M11-playtest-fixes-tasks.md)** · **[playtest →](../playtests/2026-09-10.md)**
The first extended play after the plan finished, and it found more than five
milestones of tests had. A taken jar stayed in the room description forever,
because the narrator was shown items and its prose was cached for the life of
the world. `search` re-described the room and filed it as a discovery — all ten
in that session's world were keyed on the room's own name. "Braced" was named
for the opposite of what the judge meant, printed twice, and did nothing. The
TUI bound Textual's copy key to quit. Nothing wrote a transcript.

The pattern underneath most of it was the tier-1 parser fallback: players
reached for `search`, `hide`, `equip`, `enter`, and each one cost a model call
and got routed by a small model that lifts targets from the room. Making them
real verbs was the highest-leverage fix. Replaying the playtest's own commands
against the world it was played in:

| | before | after |
|---|---|---|
| tier-1 calls | 13 | **1** |
| jar in the description after `take` | yes | no — in the old world too |
| `search` | re-described the room | a fixture the room hadn't named, or "nothing more", free |
| `hide`, `equip` | the judge, inventing | code, free |

**The replay found one more bug, live since M10.** `persist_floor` wrote each
room node's data wholesale on every run, erasing the index M10 keeps there — so
a discovery could be replayed only in the run that made it, and the next search
filed the same find again as something new. M10's test had skipped `begin()`,
the one step that persists a floor. It's the fourth bug of one shape: a node's
data blob is shared by several systems, and two of the four were writers
replacing it instead of merging.


**M11.1 — Second playtest** ✅ *done* · **[task record →](../tasks/M11.1-fixes-tasks.md)** · **[playtest →](../playtests/2026-09-10b.md)**
The first sessions played outside the `simulacra` theme. The worst thing they
found: `dodge`, which the parser didn't know, came back from the model fallback
as a *move* — through an exit the player never named, into the room that killed
them. `jump`, `slam` and `eat tin of peaches` came back as the room description.
The fallback's verb list had grown from 10 to 18 across M7–M11, and its guards
checked that a direction was real rather than that the player typed it. It may
now only answer `look` or `move` when the player looked or typed the direction;
anything else is an improvisation, and the judge rules on it.

Live, on a copy of that world: 12 of the 13 fumbled commands landed, and none
moved the player. The thirteenth, `/exit`, found a hole M11 had opened — `enter`
falls back to looking at its target, and a target lifted from the room summary
is the room itself. Also fixed: transcripts named per world (three worlds' runs
had appended into one file), "(remembers you)" meaning a previous run only, the
NPC prompt reading as a sentence, `drink`/`eat` using the only thing you carry,
and `d` saying where the stairs are once you've found them.

The playtest also settled M12's premise: mirrors and altars turned up in a
silver mine because the engine sends every theme the same room-role words
(`shrine`, `vault`, `lair`) and stamps every floor motif onto every room.


**M12 — Variety** ✅ *done* · **[task record →](../tasks/M12-variety-tasks.md)**
Both 2026-09-10 playtests saw the same image everywhere — mirrors on every floor
of a copy-of-a-vanished-place, and mirrors, altars and keys in a silver mine.
The second theme settled the cause: it was the engine. Every theme got the same
room-role words (`shrine` reads as altars; `vault` as keys), every room's prompt
carried every floor motif and the theme's whole motif list, and the director's
stock names ("Chamber of Shadows", "Lair of the Forgotten") overwrote both
themes' own.

The model is now told what a room *does* in the theme's words or a neutral
default, never the engine's role name; each room gets one mood, rotated; motifs
must be moods rather than objects; stock names, repeated floor names and
repeated moods are refused in code; and room ids are stripped from anything the
director writes. Measured on fresh worlds with fixed seeds, the real model,
before and after — Hardpan on the engine's neutral defaults alone:

| | Hardpan before | after | simulacra before | after |
|---|---|---|---|---|
| words in half a floor's rooms | 64 | **21** | 31 | **8** |
| `mirror` per 1k words | 9.1 | **1.1** | 10.4 | 5.6 |
| `altar`/`glass`/`shattered` | present | **0** | present | glass only |
| stock director room names | 4 of 5 | **0 of 14** | 8 of 8 | **0 of 10** |

The baseline also showed the director dodging its own avoid-list by numbering —
*"Erebus's Veil"*, *"…II"*, *"…III"* — so floor names are now compared by stem.


## 10. State of the repo

Built and verified:

- [config.py](../../src/simulacra/config.py) — settings, per-call-site model policies
- [llm/client.py](../../src/simulacra/llm/client.py) — full Ollama client + telemetry
- [llm/doctor.py](../../src/simulacra/llm/doctor.py) — `python -m simulacra.llm.doctor`, probes
  and benchmarks the setup against the §2 tier budgets
- [memory/store.py](../../src/simulacra/memory/store.py) — SQLite + vec + graph, **smoke-tested**
- [world/floorgen.py](../../src/simulacra/world/floorgen.py) — invariants hold depths 1–25, plus pacing guard (§11)
- [world/theme.py](../../src/simulacra/world/theme.py) + [themes/simulacra.toml](../../themes/simulacra.toml)
- [world/model.py](../../src/simulacra/world/model.py), [engine/events.py](../../src/simulacra/engine/events.py) — the type boundaries
- [engine/parser.py](../../src/simulacra/engine/parser.py) — stage 1, table-driven
- [engine/state.py](../../src/simulacra/engine/state.py) — run bootstrap + graph persistence
- [engine/loop.py](../../src/simulacra/engine/loop.py) — turn loop, `_describe()` swap point
- [ui/repl.py](../../src/simulacra/ui/repl.py) — streaming renderer
- [ui/tui.py](../../src/simulacra/ui/tui.py) — the second renderer: panels, worker thread, fog-of-war map
- [narrate/narrator.py](../../src/simulacra/narrate/narrator.py) — streamed prose + echo guard
- [narrate/prefetch.py](../../src/simulacra/narrate/prefetch.py) — serial generate-ahead worker
- [world/director.py](../../src/simulacra/world/director.py) — tier-3 floor identity
- [memory/writer.py](../../src/simulacra/memory/writer.py) — the write path, an observer
- [__main__.py](../../src/simulacra/__main__.py) — playable: `simulacra --seed 42` (`--offline` for no model, `--ui tui` for panels)

Nothing is stubbed. Every milestone in §9 is implemented and tested.

```
uv sync                       # the project plus the dev group: pytest and Textual
uv run pytest                 # no model needed
uv run simulacra --seed 42    # the REPL: scriptable, pipeable, CI-friendly
uv run simulacra --ui tui     # the panelled frontend
uv run pytest -m llm          # opt-in, needs the daemon
```

The test tools and Textual are a uv *dependency group*, not extras: `uv run`
keeps default groups and strips extras, so as extras every plain `uv run` quietly
uninstalled them (M11.1). An install outside a checkout gets the TUI with
`pip install simulacra[tui]`.

---

## 11. Open risks

**Prose quality: resolved well enough to build on.** Across two full M2 runs the
narrator produced consistently readable, on-theme description and **the echo
failure never reached the player once**. The guard in
[narrator.py](../../src/simulacra/narrate/narrator.py) works: labelled census so
an echo is detectable, a 48-character prefix check so a bad generation is cut off
after ~3 tokens, one strict retry, then a procedural fallback. Remaining nit: the
model writes 45–90 words against a theme brief asking for two sentences and forty
words. It reads fine and streams faster than the player reads, so it is a tuning
question, not a defect.

**Two bugs found by running M2, both fixed.** A hot spin in the prefetch worker
(`Event.wait()` returns immediately when the event is *set*, so waiting on the
cancel flag to mean "wait until preemption ends" burned a core — 3.7 M loop
iterations in four turns, stolen directly from CPU inference); and the director
overwriting the theme pack's room names with restatements of the structural role
("Entrance", "Chamber"). Both now have regression tests.

**Historical — the original wording of this risk, kept for context.**
Latency is solved and confirmed (`doctor` measures tier 2 at ~2 s and tier 1 at
~4.8 s, both inside budget). Quality is not. Across two `doctor` runs on
`qwen3:1.7b` the narrator prompt produced good prose once and **echoed the input
census back verbatim** the other time — the model intermittently fails to follow
the system brief at all. The narrator will therefore need an output guard
(reject-and-retry when the response overlaps the prompt, fall back to the
procedural room name on a second failure), not just a good prompt.

Mitigations in rough order of cost: restructure the narrator prompt so the census
is clearly data rather than text to continue → tighten the theme brief → widen the
`banned` list → few-shot with 2–3 exemplars in the theme pack → fall back to
`qwen3.5:2b` at ~10 tok/s (still faster than reading) → pre-generate floors
offline where thinking becomes affordable.

**The judge is harsh and shallow.** Both `doctor` runs rated "tip the brazier into
the flooded room" at difficulty **20** (the schema maximum) with `damage_self` —
punishing exactly the creative play the judge exists to reward. This is the §7
rationale arriving on schedule: the clamp keeps it from breaking the game, but M3
will need difficulty calibration (likely few-shot anchors showing what a DC 10 vs
DC 18 action looks like).

**The 31 s cold load is user-visible exactly once**, at startup. Warm during the
title screen so it overlaps with the player reading the intro.

**Prefetch depends on branching factor.** A room with six exits can't have all
neighbours pre-narrated in time. Prioritise by the direction the player is
already heading; accept an occasional live stream, which is still only ~12 s and
still outpaces reading.

**Memory relevance will need tuning.** Three recollections is a starting guess.
Too few and NPCs feel amnesiac; too many and prompts bloat and the model starts
reciting a list instead of speaking.

**Loops short-circuited the spine — found by playing M1, now fixed.**
`floorgen` builds a guaranteed spine and *then* adds loop edges, and nothing
stopped a loop wiring the entrance straight to the descent. Since the vault, lair
and shrine all sit on the spine, those runs skipped the floor's entire content.

Fixed with `MIN_TRAVERSAL = 0.7` in
[floorgen.py](../../src/simulacra/world/floorgen.py): a candidate loop edge is
added, then rejected if it drops the entrance→descent distance below that
fraction of the spine. Measured over 300 seeds per depth:

| depth | rooms | steps before → after | content on route | runs skipping **all** content |
|---|---|---|---|---|
| 1 | 5 | 2.9 → **3.1** | 62% → **70%** | 24% → **0%** |
| 4 | 7 | 2.9 → **5.0** | 35% → **78%** | 40% → **0%** |
| 8 | 9 | 3.2 → **6.0** | 29% → **67%** | 43% → **0%** |
| 12 | 11 | 3.3 → **7.0** | 25% → **61%** | 41% → **0%** |

Route length now scales with floor size instead of sitting flat at ~3, and no
seed at any depth skips every content room. Loops survive — 0.9 extra edges per
floor at depth 1 rising to 4.5 at depth 12 — so floors are still graphs, not
hallways. Four tests lock this in, including one asserting the guard did not
reject loops into nonexistence.

**Decided: floors persist.** Resolved in [M6](../tasks/M6-persistent-world-tasks.md): the world seed decides layout at every depth and the model's contribution is stored and re-attached, so run 2 of a world walks the same floors at no model cost. This paragraph still said "deferred to M4" until the M11 closeout found it.