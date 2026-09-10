# M9 — Action vocabulary: trade, gifts, disposition, movement · ✅ COMPLETE

> **Status: done.** 648 tests green with the daemon unreachable. Findings —
> including a latent bug that had been erasing NPC state since M4 — below.

> Scoped from [systems.md §8](../plan/systems.md#8-s4--action-vocabulary).
> **Depends on M7** (NPCs are entities with an inventory and a disposition) and
> **M8** (the `trade` route has somewhere to resolve into).

**Goal:** a gift changes what an NPC will tell you. Things change hands, NPCs
hold opinions, and one of them will walk with you.

**Why now:** every milestone so far has made NPCs *know* more. None has made
them *do* anything. M7 gave them an inventory and a disposition scalar and
nothing has ever written to either.

**Constraint:** tier 1 — one small structured call per social decision, on a
verb the player typed deliberately. It must not touch movement, combat, or any
other turn.

---

## A correction to carry into this milestone

[systems.md §8](../plan/systems.md#8-s4--action-vocabulary) says the action
vocabulary is "generalised from improvised player actions to cover NPC decisions
and monster behaviour under **one** vocabulary rather than three subsystems."
Two thirds of that is right and one third is not.

**Monster behaviour stays in code.** Routing monsters through a tier-1 call
would put one on every combat turn, against a budget that reserves tier 1 for
verbs the player typed. [combat.py](../../src/simulacra/engine/combat.py) is
dice, seedable and testable without a model, and it should stay that way.

**And one schema does not fit both callers.** The judge resolves a *physical*
action with dice — `plausible`, `difficulty`, `magnitude`. An NPC deciding
whether to accept a gift is a *social* choice with no dice in it. Forcing one
schema over both would make each worse.

What is genuinely shared is the **architecture**, and that is the part worth
generalising: a closed enum, code validating against state the model does not
get to assert, and code clamping the result. M3 measured that pattern going from
3-of-5 to 5-of-5 by splitting one six-way enum into two simple choices; M9 is its
second instance, not its second copy.

---

## A1 — The vocabulary

**Files:** new `engine/dealings.py`

One structured call, one closed enum, in the shape
[judge.py](../../src/simulacra/engine/judge.py) already validates:

```python
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "act": {"type": "string", "enum": ["accept", "refuse", "give", "warn", "nothing"]},
        "object": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["act", "object", "reason"],
}
```

Five choices, not nine. `move`/`follow`/`lead` are A5 and are not decided by the
model — the player asks, and disposition answers, which is code's call. `attack`
is not in an NPC's vocabulary at all: an NPC that can decide to attack is a
monster, and [floorgen](../../src/simulacra/world/floorgen.py) already has those.

**The model never mutates state.** It returns a decision; `apply()` validates it
against what is actually true — does the NPC hold that object, is the item real,
is disposition sufficient — and only then moves anything. A 1.7b asked to
freeform an exchange will invent items, and inventing an item is a duplication
bug in a world that persists.

**Failure is `refuse`, never `accept`.** A broken call must not be able to
hand the player something. Same rule as the judge's *implausible*.

**Tests:** an invented object refuses; an `act` outside the enum refuses; a raised
exception refuses; nothing in the store changes on any of those.

---

## A2 — Giving

**Files:** [engine/loop.py](../../src/simulacra/engine/loop.py), `dealings.py`

`give <item> to <npc>`. Code checks the player actually holds the item — that is
not a question for the model — then asks the NPC to `accept` or `refuse`.

On accept: the item moves from `player.inventory` to the NPC node's `inventory`,
and **disposition rises**. On refuse: nothing moves, and the NPC says why.

**Disposition moves by a fixed step, not by a model-supplied number.** Letting
the model score a gift is the M3 mistake again — it will rate everything
maximally and quietly make disposition free.

**Tests:** giving something not held is refused with no model call; an accepted
gift leaves the player's inventory and appears in the NPC's; disposition rises
exactly one step; a refused gift moves nothing.

---

## A3 — Asking for something

**Files:** `dealings.py`, `engine/loop.py`

`ask <npc> for <item>`. The mirror of A2, and the one that makes disposition
worth having: the NPC decides `give` or `refuse`, and code gates it on
disposition **before** the model is consulted — a wary NPC refuses for free.

Note the parser collision: `ask` is already a `talk` alias. `ask X for Y` has to
route to this and `ask X about Y` to conversation, which is A7's job and is
exactly the split [R1](M8-routes-tasks.md) built the addressee slot for.

**Tests:** a wary NPC refuses without a model call; a warm one gives, and the
item actually moves; asking for something the NPC does not hold refuses without
a model call.

---

## A4 — Disposition, and what it buys

**Files:** `dealings.py`, [engine/routes.py](../../src/simulacra/engine/routes.py)

An integer on the NPC node, already there since M7 and never written to. Three
bands, and code owns every transition:

| Band | How you get there | What it means |
|---|---|---|
| wary (< 0) | attacking them, a refused threat | refuses trade outright, no model call |
| neutral (0) | the default | everything M8 already does |
| warm (≥ 2) | gifts | `derived` canon unlocks |

**A gift buys access to a route, not an item** — systems.md's phrasing, and the
reason this is more interesting than a shop. Concretely: the `self` route returns
`authored` canon to anyone and adds `derived` canon only when warm. A stranger
gets the official line; someone who has given them something gets the personal
history the canonist worked out.

**This must not regress an earlier gate.** M7's gate is "a backstory that is not
a death", which is authored canon and stays available at neutral. M4's gate is
the Archivist remembering your last run, which is the `past` route and stays
available at neutral. Only `derived` moves behind a gift, and only *wary* takes
anything away. Check both gates still pass.

**Disposition persists.** It is on the node, so it outlives the run — which is
the whole point, and also means a player who attacks an NPC has done something
lasting. `--forget` resets it.

**Tests:** each band's gate; disposition survives a new run; `--forget` resets
it; the M4 and M7 gates still hold at neutral.

---

## A5 — Follow: movement as state

**Files:** `engine/loop.py`, [engine/state.py](../../src/simulacra/engine/state.py)

M7 claimed that making placement read the node "is what makes M9's NPC movement
a state update rather than a floorgen rewrite". This is the task that cashes
that claim, and it should be small. If it is not small, the claim was wrong and
that is the finding.

`follow me` / `wait here`, gated on disposition (warm follows, neutral declines,
wary refuses). A following NPC moves with the player within its floor, and its
`room_id` is written on each move, so it is **where you left it** on the next
run — including if you left it somewhere it does not live.

**It does not follow downstairs.** An NPC has a `home_depth`; taking the
Archivist to floor 6 would be a different feature, and one that needs an answer
for what happens when you die there.

**Tests:** a warm NPC follows and its stored room updates; a neutral one
declines; it stays behind at a descent; a followed NPC is in the room it was led
to on the next run.

---

## A6 — The `trade` route

**Files:** `engine/routes.py`

M8 deliberately left this out — "adding a route that returns nothing would be a
stub pretending to be a feature." Now it has somewhere to resolve into: what the
NPC is holding, and what it will do about it at the current disposition.

**Tests:** the route resolves to the NPC's inventory; a wary NPC's trade route
resolves to nothing, which the M8 machinery already turns into a spoken refusal.

---

## A7 — The parser learns the social verbs

**Files:** [engine/parser.py](../../src/simulacra/engine/parser.py)

`give`, `offer`, `show`, `hand` → `give`. `follow` handled as a `tell`-shaped
verb. And the `ask X for Y` / `ask X about Y` split.

`split_address` already handles connectives; `for` is currently in `_TOPIC_WORDS`,
so `ask archivist for the blade` splits as addressee `archivist`, topic `blade` —
correct data, wrong verb. The verb has to change on seeing `for`.

**Tests:** the split table extended, including `ask X for Y` reaching `request`
and `ask X about Y` reaching `talk`.

---

## A8 — Measure

**Files:** a note appended to [plan.md §9](../plan/plan.md#9-milestones).

| | target |
|---|---|
| tier-1 calls per social turn | ≤ 1 |
| refusals that cost a model call | 0 (disposition gates first) |
| turns other than the social verbs affected | 0 |

And the qualitative check, in the manner of M7 and M8: **give an NPC something,
then ask it the same question you asked before.** If the answer does not change,
disposition is not buying anything and A4 is decoration.

---

## Order and dependencies

A1 → A2 → A3. A4 needs A2 to have something that moves it. A5 and A6 need A4.
A7 is needed by A2/A3 to be reachable at all, so it lands early in practice.

Suggested order: **A1, A7, A2, A4, A3, A6, A5, A8.**

## Definition of done

- [ ] A gift changes what an NPC will tell you, verified live
- [ ] Items actually move, and cannot be invented by the model
- [ ] A wary NPC refuses without spending a model call
- [ ] Disposition survives a run, and `--forget` resets it
- [ ] An NPC led somewhere is there on the next run
- [ ] The M4 and M7 gates still pass at neutral disposition
- [ ] No non-social turn gained a model call

## Watch for

**Inventing items.** The model returns an `object` string. If code trusts it,
an NPC can hand over something that never existed, permanently, in a world that
persists. Match against real item ids only, and refuse otherwise.

**Disposition inflation.** A fixed step per gift, and a cap. Without a cap the
player empties their pack into an NPC and buys everything, which makes the bands
meaningless.

**The refusal path must stay free.** M8's finding was that a refusal is code's
job. Every refusal that disposition can decide should be decided before the
model is asked anything — otherwise M9 quietly puts a tier-1 call on turns that
had none.

**A5 proving M7 wrong.** If movement turns out to need floorgen changes after
all, then C3's inversion did not buy what it claimed. Say so plainly rather than
quietly making it work.

---

## Findings

### `upsert_node(data=None)` was erasing NPCs on every conversation turn

The one that matters. `_talk` touches the NPC's node to update `last_run`:

```python
self.store.upsert_node(actor.id, "npc", actor.name, run_id=self.state.run_id)
```

`data` defaults to `None`, and `upsert_node` wrote `json.dumps(data or {})` —
so a touch **replaced the node's entire blob with `{}`**. Every time the player
spoke to an NPC, its disposition, inventory, home room and canon gate were
erased.

Latent since M4, when that line was written and NPC nodes carried nothing.
Invisible through M7, which put `canon_memories` and `last_canon_run` there —
the canonist's skip-if-nothing-new gate has been quietly half-working ever
since, re-writing canon it had already written. Found by M9 only because a gift
stopped counting between one turn and the next.

Fixed at the root: `data=None` now means *leave the blob alone*, and an explicit
`{}` still clears. Two regression tests, one at the store and one through a real
conversation turn.

Worth noting the shape, because it is the third of its kind in four milestones:
a helper written when one caller existed, whose default became wrong when a
second caller arrived, failing silently. M7's canonist had no output guard;
M8's echo guard had the wrong rule for dialogue; M9's node touch had the wrong
default. None was caught by a test written at the time.

### `ask X <question>` had no question in it

`split_address` read a connective-less `talk` target as entirely an address, so
`ask archivist what are you carrying` produced addressee `"archivist what are
you carrying"` and an **empty topic** — which routes to `past` and answered a
question about trade with a death.

Connective-less now reads as name-then-question when there is more than one
word, the same rule `tell` already used. `talk to archivist` is unaffected: the
connective strips first and one word is left.

### The `reason` field is not prose

`DECISION_SCHEMA` has a `reason` so the model has somewhere to put its
rationale, and speaking it aloud put fragments on screen: *"accepted for
safekeeping"*, *"delver requested water, and I have it."* — debug output sitting
next to prose the narrator wrote.

`presentable()` checks it is three words, capitalised, and ends in punctuation,
and otherwise falls back to a deterministic line. Same discipline as every other
output guard in the project.

### `WARM` lowered from 2 to 1

Two gifts is more than a floor usually offers, and a band nobody reaches is the
same as not having one. One meaningful gift crosses it; `ATTACK_STEP` of -3
still puts you wary immediately, which is the asymmetry worth keeping.

### A5 cashed M7's claim, in nine lines

M7 said inverting placement so `floorgen` proposes and the world disposes "is
what makes M9's NPC movement a state update rather than a floorgen rewrite".
`_bring_followers` is nine lines, touches no generation code, and an NPC led
somewhere is standing there on the next run. The claim was right.

### The measurement

| | target | measured |
|---|---|---|
| tier-1 calls per social decision | ≤ 1 | **1** |
| refusals costing a model call | 0 | **0** — every one is 0.0 s |
| non-social turns gaining a model call | 0 | **0** |

And the qualitative check — the same question, before and after a gift:

```
[disposition 0]
  You have kept the ledger since before the current numbering began.
  You will not go below the third floor, and will not say why.

[disposition 1]
  The ledger is yours. You do not speak of why you stay on the third floor.
  You number both doors and delvers.
```

The third sentence is `derived` canon, and it only exists at the warm band. A
gift buys a route, not an item.

### The correction at the top held up

Monster behaviour stayed in `combat.py`; one schema did not fit both callers.
What generalised was the architecture — closed enum, code validates against real
state, code clamps — and `dealings.py` is its second instance rather than its
second copy. [systems.md §8](../plan/systems.md#8-s4--action-vocabulary) is
amended to say so.

### Known limitations

**An NPC will not follow you downstairs.** It has a `home_depth`, and taking the
Archivist to floor 6 needs an answer for what happens when you die there.

**Nothing lowers disposition in play yet.** `ATTACK_STEP` exists and is unused:
attacking an NPC is refused outright by `_attack`, so there is no path to wary
except the debugger. That is a gap, not a bug — the wary band is fully built and
tested, and wiring the attack path is a small follow-up.
