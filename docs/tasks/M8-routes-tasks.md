# M8 — Retrieval routes + conversation session · ✅ COMPLETE

> **Status: done.** 610 tests green with the daemon unreachable. Findings —
> including one that changed the shape of the design — at the bottom.

> Scoped from [systems.md §6](../plan/systems.md#6-s2--retrieval-routes) and
> [§7](../plan/systems.md#7-s3--conversation-session). **Depends on M7** — the
> routes retrieve from canon, and the session dedupes what they surface.

**Goal:** eight questions get eight answers, and the one fact worth saying gets
said once. An NPC asked about the corridor answers about the corridor; asked
about something it has no knowledge of, it says so; asked twice, it says it has
already told you.

**Why one milestone for two systems.** Routes without a session still repeat
facts — they just retrieve better things to repeat. A session without routes has
nothing worth deduping, because every question resolves to the same three
memories. They are independently *testable* and not independently *valuable*,
and shipping either alone would read as no improvement.

**Constraint:** tier 0, escalating to tier 1 on a stage-1 miss. Talk turns are
tier 2 today; the worst case after this is tier 1 + tier 2. The stage-1 keyword
table is the entire mitigation and **its hit rate is the number that decides
whether this design holds** (R8).

---

## Non-goals

| Deferred | Milestone |
|---|---|
| The `trade` route, and disposition *changing* | M9 |
| Room and item canon written by discovery | M10 |
| Contradiction detection between canon rows | — (M7 finding) |
| Multi-turn planning, NPCs asking questions back | — |

The `trade` route is named in systems.md §6 and is deliberately absent here: it
resolves into the action vocabulary, which does not exist yet. Adding a route
that returns nothing would be a stub pretending to be a feature.

---

## What already exists

- `Store.canon(node_id, provenance=...)` and `Store.recall(embedding, about=...)`
  — both resolvers, already written and tested (M7, M4).
- `Store.neighbors()` — written in M0, **still called by nothing in the engine**.
  R4 is the first code that reads the graph, which is the third finding of
  [systems.md §1](../plan/systems.md#1-why-the-conversation-work-stalled).
- The two-stage parser shape (`parse` → `infer`), which R3 copies exactly.
- `_addressed()` in loop.py — M7's word-overlap matcher, which R1 moves half of.

---

## R1 — The parser gets a topic slot

**Files:** [engine/parser.py](../../src/simulacra/engine/parser.py),
[engine/loop.py](../../src/simulacra/engine/loop.py)

The first finding of the review, and the reason `_topic_of()` exists: `Intent`
is `verb` + `target`, so `ask archivist about the arm` arrives as one fused
string and gets pulled apart again with three ordered stopword passes inside a
verb handler. Trade and gifts need the same split for `give`, `show` and
`offer`, so it has to stop being a special case of `talk`.

Add `addressee: str = ""` to `Intent`. For the `talk` and `tell` verbs the
parser performs the **syntactic** split — address words, then the first topic
word — and leaves `target` holding the topic alone:

| input | addressee | target |
|---|---|---|
| `talk to archivist` | `archivist` | `` |
| `ask archivist about the arm` | `archivist` | `arm` |
| `ask about the arm` | `` | `arm` |
| `tell archivist the vault is open` | `archivist` | `vault is open` |

**The parser cannot do the whole job, and must not try.** Deciding *which NPC*
is a resolution problem needing the room's occupants, which is engine knowledge.
So the parser splits on connectives only, and `_addressed()` keeps matching the
addressee phrase against the actors present. What moves is the syntax; what
stays is the resolution.

**`tell` has no connective to split on.** `tell archivist the vault is open`
gives the addressee as the leading token. That is wrong for a multi-word name
and right for every name in the theme pack; the engine's name-aware match is the
backstop, so a miss degrades to "which of them?" rather than to the wrong NPC.
Note it rather than solving it.

**Delete `_topic_of()`.** Its three passes are the thing this task exists to
remove. `DEATH_QUERY` stays — it is the `past` route's query when the player
named no topic, which is still good behaviour and still what
`test_the_archivist_remembers_the_previous_run` asserts.

**Tests:** the table above, exactly; `_topic_of` is gone; a bare `talk` gives
both fields empty.

---

## R2 — `Fact`, and the route vocabulary

**Files:** new `engine/routes.py`

Every resolver returns the same thing, because the session has to dedupe across
all of them uniformly:

```python
@dataclass(frozen=True)
class Fact:
    key: str    # stable identity, for "have I said this already"
    text: str   # what goes in the prompt
```

`key` is what makes S3 work at fact level rather than at phrasing level.
Canon rows key as `canon:<id>`, memories as `memory:<id>`, live world state as
`monster:<actor id>` or `exit:<direction>` — stable across turns within a run,
which is all the session needs.

**`Recollection` gains an `id`.** `Store.recall` already selects `m.*`, so the
row is there and the dataclass simply drops it; without it a recalled memory has
no stable key and the session can only dedupe on text.

The routes:

| Route | Resolver reads | Vector search |
|---|---|---|
| `self` | the NPC's own canon (`authored` + `derived`) | no |
| `room` | live room census + `room:` node data | no |
| `monsters` | graph: hostiles here and one hop out | no |
| `npc` | graph: other NPC nodes in `met_npcs`, and their canon | no |
| `item` | items in the room and in inventory | no |
| `past` | episodic recall — today's `_recall_for`, unchanged | **yes** |
| `unknown` | nothing | no |

**Six of seven never embed anything.** That is the efficiency claim and R8
measures it. The engine already knows exactly what is in the room; running a
768-dimension nearest-neighbour search to find out is the thing this milestone
deletes.

---

## R3 — Classification, two-stage

**Files:** `engine/routes.py`

Stage 1 is a keyword table and costs nothing:

```python
_TABLE = {
    "room": {"room", "here", "place", "door", "corridor", "exit", "way", "wall"},
    "monsters": {"monster", "thing", "creature", "danger", "safe", "hostile"},
    "npc":  {...names from the roster..., "anyone", "someone", "else"},
    "item": {"item", "key", "blade", "water", "ration", "carry"},
    "self": {"you", "your", "yourself", "name", "who"},
    "past": {"delver", "died", "death", "before", "last", "happened"},
}
```

Stage 2 — **only on a miss** — is a ~32-token structured call against the same
enum, in the shape [`parser.infer`](../../src/simulacra/engine/parser.py) already
proves. If this lands on the common path, the tier-0 claim is gone.

**Empty topic is not a miss.** `talk to archivist` names no topic, and that is
the `past` route by the same reasoning `DEATH_QUERY` was chosen for in M4 — an
NPC greeted with nothing to go on volunteers the most important thing it knows.
Route it directly; never spend a tier-1 call deciding that nothing means
something.

**Names come from the theme pack, not a literal.** The `npc` keyword set is
built from the roster at construction, so a new NPC is routable without a code
change.

**Tests:** each keyword set routes; a phrase in none of them escalates exactly
once; an empty topic routes to `past` with zero model calls; classification
never raises (a failed stage 2 degrades to `unknown`, which is an honest "I
don't know", not an error).

---

## R4 — The resolvers

**Files:** `engine/routes.py`

Each is a plain function of `(state, store, theme, npc, topic) -> list[Fact]`.
Two of them are the first engine code to traverse the graph:

- **`monsters`** — hostiles in this room, then `neighbors(room, "EXIT_*")` one
  hop out. This is the route that most obviously should never have been a vector
  search.
- **`npc`** — other roster NPCs the player has met (`state.met_npcs`), plus their
  `authored` canon. The Corrector's *"Does not trust the Archivist's count"* is
  the material this route exists to surface, and it is why M7 added a second NPC.

**A resolver that finds nothing returns `[]`, and that is the whole of T2.**
No distance threshold, no calibration against a live embedding model, no guard
against an embedding outage reading as maximal relevance. The cancelled task
would have spent a milestone tuning a cutoff; an empty list is a truth about the
data.

**Every resolver is total and cheap.** No exceptions escape, and none of them
except `past` may make a model call — asserted by a test, because this is the
property that keeps talk turns off tier 1.

---

## R5 — The conversation session

**Files:** `engine/loop.py`

```python
@dataclass
class Conversation:
    npc_id: str
    exchanges: list[tuple[str, str]]   # (asked, said), last 2
    surfaced: set[str]                 # Fact keys already spoken
    open_route: str = ""
```

Held in `Engine`, keyed by npc id, **never touching `Store`**, never embedded,
gone when the run ends. That is T3's one load-bearing constraint and it carries
over unchanged: M4's bug happened because a reply reached *persistent* memory,
and a same-run scratch buffer cannot reproduce it.

`surfaced` is why this is not T3. T3 proposed a window of raw text so the model
could see what it had said. This tracks **which facts** were spoken, by key —
so when a route resolves entirely to keys already in `surfaced`, the NPC says it
has already told you, decided in code rather than asked of a 1.7b.

**Mark facts surfaced only when the reply actually happened.** The echo and
degeneracy guards can swallow a whole generation, and a fact marked as said
after the NPC said nothing is a fact the player can never hear.

**Tests:** the same question twice takes the repeat branch on the second; a
different route in between does not; the dict is empty after `RunEnded`; nothing
reaches `Store`.

---

## R6 — The brief reaches the prompt

**Files:** `engine/routes.py`, [narrate/narrator.py](../../src/simulacra/narrate/narrator.py)

The route decides what is retrieved *and* what the model is told to do with it.
That pairing is the T2 fix, so it travels as one object:

```python
@dataclass
class Brief:
    route: str
    canon: list[str]        # always present: what this NPC is
    told: list[str]         # hearsay, labelled
    facts: list[Fact]       # the route's own resolution
    label: str              # section header for `facts`
    instruction: str        # closed, one sentence
    repeated: bool
```

`Narrator.npc()` renders it and keeps every existing guard. Per-route
instructions, all terse and closed, in the register M4 measured as working:

| Route state | Instruction |
|---|---|
| `past` with recollections | *(M4's wording, unchanged — it was arrived at the hard way)* |
| any route with facts | "Answer using only what is listed. Do not invent anything else." |
| `self` | "Answer them from what is true of you. Do not invent history." |
| resolved to nothing | "You do not know anything about that. Say so briefly, in character." |
| `repeated` | "You have already told them this. Say so, briefly, and do not repeat it." |

**Every branch ends with an instruction.** M7 learned this the expensive way:
dropping the instruction from one branch left a prompt of pure data, which a
1.7b answers by transcribing. There is no branch here without one.

**Hearsay attribution moves into the resolver.** M7's finding was that the rider
"if you repeat any of that, say who told you" is mostly ignored. Say it in the
fact instead — `A delver told you: <claim>` — so the attribution is in the data
rather than in an instruction the model may skip.

**Token budget.** More sections, but each one smaller, and only one route's
facts are ever present. Count it with every cap full, as M7 did.

---

## R7 — Delete what the routes replace

**Files:** [engine/loop.py](../../src/simulacra/engine/loop.py)

- `_topic_of()` — R1.
- The unconditional recite instruction in `Narrator.npc()` — R6 makes it the
  `past` route's instruction, which is where it always belonged.
- `_canon_for` / `_told_to` become the `self` resolver and the hearsay section.
- `CANON_LIMIT` / `TOLD_LIMIT` move to `routes.py` beside the resolvers that
  enforce them.

A milestone that only adds is a milestone that left its predecessor in place.
The M7 code these replace has no other caller — check, and remove it.

---

## R8 — Measure

**Files:** a note appended to [plan.md §9](../plan/plan.md#9-milestones).

**The load-bearing number is the stage-1 hit rate.** Play a real conversation of
a dozen varied questions and count how many classify without a model call.

| | target |
|---|---|
| stage-1 route hits | **≥ 70%** |
| talk turns making a tier-1 call | the remainder, and no more |
| routes other than `past` making any model call | **0** |
| dialogue prompt, worst case | inside the ~600-token tier-2 budget |

If stage 1 lands below 70%, the two-stage design is not paying for itself and
classification should move *into* the existing stage-2 parser call rather than
being a second one. Decide that on the measurement, not on taste.

And the qualitative check, in the manner of M7's five-run read: **ask one NPC
eight different questions in one conversation and read the transcript.** The
live sample in [conversation-system.md](../conversation-system.md) — eight
questions, one fact, eight phrasings — is the before. If the after still circles
one fact, the routes are classifying but the resolvers are thin, and that is an
input-selection problem, which M7 already established is where the quality is.

---

## Order and dependencies

R1 → R2 → R3 → R4 is a chain. R5 needs R2's `Fact`. R6 needs R4 and R5. R7 is
the cleanup pass and R8 is last.

Suggested order: **R2, R1, R3, R4, R6, R5, R7, R8** — `Fact` first because
everything else names it, and R6 before R5 so a route is visibly answering the
right question before the session starts suppressing anything.

## Definition of done

- [ ] Eight different questions in one conversation retrieve seven different
      things, and the death is not one of them unless asked for
- [ ] "What's down that corridor" is answered without an embedding call
- [ ] A question the NPC has no knowledge of gets an in-character "I don't
      know" — with no distance threshold anywhere in the code
- [ ] The same question twice gets "I've told you" the second time
- [ ] Session state never reaches `Store`, and is gone on `RunEnded`
- [ ] Stage-1 hit rate measured and recorded, with the ≥70% call made on it
- [ ] `_topic_of` and the unconditional recite instruction are deleted
- [ ] The tier-2 prompt is inside budget with every cap full

## Watch for

**The classifier becoming the quality bottleneck.** A wrong route is worse than
no route: it answers a question nobody asked, confidently. Prefer `unknown` to a
guess — "I don't know" is a good failure and a confident irrelevance is not.
Test the misroutes, not only the hits.

**Resolvers quietly making model calls.** The whole efficiency claim is that six
of seven routes are free. It would be very easy to have the `npc` resolver embed
something for ranking. Assert on the client's call count, not on intent.

**The session suppressing too much.** An NPC that says "I've told you" to a
rephrased question is worse than one that repeats itself — the player did not
ask the same question, they asked a question that happened to resolve the same
way. Suppress only when *every* fact for the route is already surfaced, and
never on the first turn of a conversation.

**M7's warning, still live.** The prompt gains sections again. Every branch must
carry an instruction, and the guards must be checked against the longer prompt
rather than assumed to still fire.

---

## Findings

### R8 — the numbers

Ten varied questions to the Archivist, `qwen3:1.7b` on this box, against a world
with one death in it:

| | target | measured |
|---|---|---|
| stage-1 route hits | ≥ 70% | **90–100%** across runs |
| turns that embedded anything | — | **3 of 10** |
| routes other than `past` making a model call | 0 | **0** |
| refusal / repeat turns | — | **0.0 s**, no model call at all |

The two-stage design pays for itself comfortably. The one escalation in the
first run was *"the ledger"*, which the third finding below removed.

### The refusal branch could not be a prompt

This is the finding that changed the design. R6 specified an instruction —
"You do not know anything about that. Say so briefly, in character." A 1.7b
handed that **invents an answer anyway**:

> `> ask archivist about the corrector`
> `The corrector is a device used to alter records. Its purpose is unclear.`

The Corrector is a person. Stripping the canon and hearsay out of the refusal
prompt — on the theory, from M7, that data beats instruction — did not fix it:
the model invented from the *question*. Told it had already answered, it
answered again in different words rather than saying so.

So the refusal stopped being a generation. `Brief.spoken` carries a line the
engine says verbatim, with **no model call**, and the theme pack owns the
wording (`refusal` and `repeat` on the roster entry) while code owns that it
happens. That is [plan.md §7](../plan/plan.md#7-resolution-the-llm-proposes-code-disposes)
— *the LLM proposes, code disposes* — arriving in conversation, where it had
never been applied. It also makes saying nothing **free**, which is the right
price for it.

Worth generalising: an instruction whose whole purpose is to *suppress* output
is the weakest possible use of a small model. Anywhere the correct behaviour is
"produce nothing new", prefer code.

### The echo guard was wrong for dialogue

`looks_like_echo` rejects output that appears verbatim in the prompt. That is
right for room prose, where the census is data to **transform** — it was written
for exactly that in M2. It is wrong for a route's facts, which the NPC is being
asked to **relay**: *"There is a smudged figure west of here"* appearing in both
prompt and reply is the system working. Measured as an NPC answering a question
about its own room with "They say nothing."

`verbatim=False` for dialogue; the label test still applies both ways. Second
time in two milestones that a guard built for one caller silently failed a new
one — M7 was the same shape.

### Fact-level dedupe alone over-suppressed

R5 suppressed when every fact for a route was already surfaced. The `past` route
collapses many different topics onto one small set of memories, so a question
the player had **never asked** got "It is already entered." — the exact failure
the task doc's own Watch-for predicted, arriving anyway.

Suppression now needs both halves: the same *question* again, and nothing new
behind it. `Conversation.asked` carries the normalised topics.

### An NPC's canon should make its own subjects routable

*"The ledger"* is a question about the Archivist — its authored canon is entirely
about a ledger — but no keyword list written in advance knows that, and the
stage-2 classifier sent it elsewhere. The `self` keyword set is now built from
the NPC's own canon at classification time, using only words no other route
claims, so canon mentioning "floor" cannot hijack every question about the room.

Same rule as the roster names: a new NPC is routable without a code change.

### Deviations from the plan

**An unclassifiable topic falls through to `past`, not `unknown`.** The vector
index is the one resolver that can answer an arbitrary topic, which is the job
systems.md §6 keeps it for. It still ends at "I don't know" when it finds
nothing, so nothing is lost and a real search happens first.

**The `past` route drops dialogue transcripts** when anything with content
survives, for the reason M7 found: a transcript records the shape of a question
and not its answer.

### The gate, live

The before, from conversation-system.md — eight questions, one fact, eight
phrasings. The after, same NPC, ten questions:

```
> talk to archivist
  The delver who was killed by a partial on floor 1 after 6 turns was numbered 17.
> ask archivist about this room
  ...The Unworn Place is sacred. There is a cracked altar and a single glowing
  crystal. Ways out are west and north.
> ask archivist about what is nearby
  ...What is nearby is a partial.
> ask archivist about the corrector
  Not in the ledger.
> ask archivist about yourself
  You have kept the ledger since before the current numbering began.
> ask archivist about the previous delver     (asked once already)
  It is already entered.
```

Ten questions, ten answers, from five different sources, three embeddings, no
tier-1 calls.

### Known limitations

**The Archivist invents delver numbers.** It reports the same death as "numbered
17" and then "numbered 32". In character for a clerk who refers to delvers by
number, and still a fabrication; the death text itself is quoted correctly.

**The degeneracy guard still occasionally eats a short reply** ("They say
nothing." for a one-item route). Rare, and the failure is silence rather than
nonsense, which is the right way round.
