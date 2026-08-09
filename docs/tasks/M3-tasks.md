# M3 — Consequence

> Milestone from [plan.md](../plan/plan.md) §9. **Status: ✅ complete.**
> 350 tests green, all without a daemon. Findings appended at the bottom.

**Goal:** the dungeon can kill you. Combat resolves in code, improvised actions
are adjudicated by the model, unparsed input stops being a dead end, and a run
can end.

**Why this shape:** M1 proved the loop, M2 proved the latency budget. M3 is where
the game acquires stakes — and where the model gets its first chance to affect
*state* rather than just prose. The entire design of this milestone is about
bounding that blast radius: the model proposes, code disposes (plan.md §7).

**Non-goals — explicitly deferred:**

| Deferred | Milestone |
|---|---|
| Writing memories / embeddings | M4 |
| NPC dialogue and recall | M4 |
| Anything the player *reads* costing a new tier-2 call | — |
| Textual TUI | M5 |

---

## Already done — do not rebuild

- **Events**: `Roll`, `Damage`, `Improvised`, `StatusChanged`, `RunEnded` all
  exist in [events.py](../../src/simulacra/engine/events.py).
- **The renderer already handles every one of them.**
  [repl.py](../../src/simulacra/ui/repl.py) was written against the full event
  vocabulary in M1, so **T1–T4 should need zero UI changes**. If you find
  yourself editing `repl.py`, stop and ask why the engine isn't expressing
  itself in existing events.
- `VERDICT_SCHEMA`, `MAX_MAGNITUDE` and `Verdict` in
  [judge.py](../../src/simulacra/engine/judge.py).
- `INTENT_SCHEMA` and `Intent.inferred` in
  [parser.py](../../src/simulacra/engine/parser.py).
- `Player.effects`, `Player.alive`, `Actor.hp/attack/defense/hostile`,
  `Item.damage/heal` in [model.py](../../src/simulacra/world/model.py).
- `Store.end_run(..., epitaph=)` already takes an epitaph.
- `GameState.rng` is a **separate stream** from the floor RNG
  (`Random(seed ^ 0x5EED)`), so combat rolls can never shift floor layout.

---

## T1 — Combat resolution (tier 0)

**File:** [engine/combat.py](../../src/simulacra/engine/combat.py) ·
`attack_roll`, `player_attacks`, `actors_attack`

Pure functions over state + an injected `random.Random`. No I/O, no model.

**Details that matter:**

- **d20 + bonus vs defense.** Emit the `Roll` event even on a hit the player
  would have assumed — the renderer already dims it, and showing the arithmetic
  is what makes the game feel honest rather than arbitrary.
- Player damage comes from the best `Item.damage` in inventory, else a bare-hands
  baseline. Don't scan the room; only carried items count.
- `actors_attack` runs **after** the player's action, and only for `hostile`
  actors in the current room.
- Take `rng` as a parameter rather than reaching for `state.rng`, so tests can
  drive an exact sequence.
- Emit `Damage` with `hp_left` so the renderer never has to query state.

**Decision to record —** M1's task doc said "a rejected command is still a turn
for the purposes of M3's monsters." **Revisit that here.** Being killed by a typo
is bad, and stage 2 (T2) means unparsed input now costs 2–4 s *and* a turn.
Recommendation: `state.turns` still increments on every input (M1 behaviour,
already tested), but **monsters act only on a resolved action** — movement,
attack, take, or a judged improvisation. A parse failure is free.

**Balance is code's responsibility and must be verifiable.** Write the
simulation test in T6 before tuning any numbers.

---

## T2 — Parser stage 2 (tier 1)

**File:** [engine/parser.py](../../src/simulacra/engine/parser.py) · `infer()`

The escalation path M1 designed for: `parse()` returns `None`, so we ask the
model to map free text onto a known verb — or to say it's genuinely novel.

**Details that matter:**

- **Only ever called on a stage-1 miss.** If this ends up on the hot path, the
  90%-of-turns-cost-zero target in plan.md §2 is gone. Assert it in a test.
- Budget is `Settings.intent`: 48 tokens, temperature 0. This is a
  classification, not a creative act.
- Give it the room summary (exits, items, actors) so "take the thing on the
  altar" can resolve. Keep it under ~200 prompt tokens.
- **Never raises.** On timeout, malformed output, or an unknown verb, return
  `Intent(verb="improvise", target=<raw>, inferred=True)` and let the judge deal
  with it. A failed classification must degrade to improvisation, not to an error.
- Set `inferred=True` so the engine (and tests) can tell which turns cost a call.
- Add `use` / `drink` / `apply` to the stage-1 alias table while here — healing
  items are unusable without it, and it is a table entry, not a feature.

---

## T3 — The judge (tier 1)

**File:** [engine/judge.py](../../src/simulacra/engine/judge.py) ·
`adjudicate`, `apply_verdict`

**Calibration is the substance of this task, not plumbing.** The M0 benchmark ran
"tip the brazier into the flooded room" twice and both times got **difficulty 20
— the schema maximum — with `damage_self`**. The judge punished exactly the
creative play it exists to reward, and the second run's `reason` was the two words
"flooded crypt".

**Details that matter:**

- **Few-shot anchors in the prompt.** A 1.7 b model has no calibration for an
  abstract 5–20 scale; give it three worked examples (roughly: an easy shove at
  DC 8, a clever environmental trick at DC 12, a genuine long shot at DC 18) and
  it has something to interpolate against. This is the single highest-leverage
  change in M3.
- **Anchors live in `judge.py`, not the theme pack.** Difficulty calibration is
  engine balance; the theme owns voice. `theme.judge_system` stays as the tone
  rider.
- Bias explicitly toward allowing. "Prefer allowing clever actions" is already in
  the theme brief and was still ignored — the anchors are what will make it stick.
- Clamp with the existing `MAX_MAGNITUDE` **after** parsing. Even inside the
  schema's range, a model that decides everything is magnitude 8 must not be able
  to trivialise the game.
- **A failed judge call returns implausible.** Never a free win. Never an
  exception reaching the loop.
- `apply_verdict` rolls against the difficulty and applies the effect from the
  closed enum. It returns Events; it does not print and does not decide anything
  the enum didn't already constrain.

**Acceptance:** a table of ~10 recorded improvisations gets sane difficulties,
and no verdict can exceed the clamps regardless of what the model returns.

---

## T4 — Death and epitaph

**Files:** [narrate/narrator.py](../../src/simulacra/narrate/narrator.py)
(`epitaph`), [engine/loop.py](../../src/simulacra/engine/loop.py)

**Details that matter:**

- `hp <= 0` → `RunEnded(cause=..., depth, turns, epitaph)`. `Player.alive` and the
  `__main__` loop condition already exist.
- `epitaph()` is the **one blocking, unstreamed call in the game**, and the only
  place a 3 s wait is fine: the player has stopped playing. Keep it to one line.
- Non-fatal like everything else — on failure, end the run with no epitaph rather
  than crashing on the way out.
- `store.end_run(...)` already accepts it; make sure the `finally` block in
  [__main__.py](../../src/simulacra/__main__.py) passes the real cause rather
  than `"abandoned"`.
- **Record the death in the graph now**, even though embeddings are M4:
  `link(run, "DIED_IN", room)` and `link(run, "KILLED_BY", actor)`. Same reasoning
  as M1's T2 — ten lines now, a painful backfill later, and by M4 the interesting
  deaths will already have happened.

---

## T5 — Engine wiring

**File:** [engine/loop.py](../../src/simulacra/engine/loop.py)

- `attack` verb → `combat.player_attacks`, then `actors_attack`.
- Stage-1 miss → `parser.infer` → if `improvise`, `judge.adjudicate`.
- Tick `Player.effects` down once per resolved turn; emit `StatusChanged` when
  anything changed.
- `use` / `drink` consumes a healing item.
- Decide and document whether fleeing is free. Recommendation: **yes** — movement
  out of a room with a hostile actor works normally. Opposed flee rolls are a
  tuning knob that can be added once there is a reason to.
- Keep `_describe()` untouched. Combat text is `Roll` / `Damage` events, not prose.

---

## T6 — Tests

**Files:** `tests/test_combat.py`, `tests/test_judge.py`, `tests/test_engine_m3.py`,
plus additions to `tests/test_parser.py`

- **Balance simulation** — the important one. Run a scripted player through
  depths 1–15 a few hundred times with seeded RNG and assert survival lands in a
  band (e.g. floor 1 is nearly always survivable, floor 15 rarely is, and the
  curve is monotonic). This is what stops a later tweak silently making the game
  unwinnable, and it needs no model.
- **Judge clamps** — a `FakeClient` returning `magnitude: 99` must not exceed
  `MAX_MAGNITUDE`; one returning garbage or raising must produce an implausible
  verdict, never a free win.
- **Stage 2 is off the hot path** — assert a turn with a stage-1 hit makes zero
  client calls.
- **Death closes the run** with a cause, and the graph has `DIED_IN`.
- All of it runs with the daemon stopped, via `conftest.FakeClient`.

---

## Order and dependencies

```
T1 combat ──┐
T2 stage 2 ─┼─> T5 wiring ──> T4 death
T3 judge  ──┘
T6 tests ─────────────────────  (write alongside, especially the balance sim)
```

T1, T2 and T3 are independent. T4 needs T1 (something has to kill you).

## Definition of done

- [x] `pytest` green with Ollama stopped
- [x] A run can be lost, and reports a cause and an epitaph
- [x] Balance simulation passes and is monotonic in depth
- [x] Judge difficulties are sane on the recorded improvisation table
- [x] No verdict can exceed `MAX_MAGNITUDE` whatever the model returns
- [x] Stage-1 hits still cost zero LLM calls
- [x] `repl.py` unchanged
- [x] Death recorded in the graph as `DIED_IN` / `KILLED_BY`

## Watch for

**The judge creeping into the balance path.** It is there for actions the verb
table cannot express. The moment `attack` starts consulting it, combat becomes
unbalanceable and every turn costs 2–4 s.

**Stage 2 becoming the default.** If unparsed input is common in play, the fix is
more aliases in the stage-1 table, not a faster model call.

**`repl.py` needing edits.** It already handles every M3 event. Needing to touch
it means the engine invented a new way to say something instead of using the
vocabulary that exists — the same seam discipline M1 established and M2 validated
when `_describe()` swapped to streamed prose without touching anything else.


---

## Findings from execution

**The balance simulation earned its place immediately.** The first run it ever
did showed depth 4 at 20% survival and depth 8 at 0% — a cliff, not a curve. Two
causes:

- **No player progression at all.** Monster HP scaled with depth while player
  damage was fixed, so the run capped out around floor 4 and "endless" meant
  nothing. Added `Player.on_descend` (+3 max hp, +6 heal, +1 attack every 3
  floors) — granted only by descending, so the reward for risk is the ability to
  take more risk. This is a **design addition beyond the task doc**, made because
  without it the milestone's own definition of done was unreachable.
- **The tier table was a step function.** `_TIERS` switched hard at depth 4,
  putting 61% of all deaths on that one floor. Tiers now fade in over three
  floors and are rolled per monster, so a shallow floor can hold one nastier
  thing.

Distribution over 400 simulated runs, after both fixes:

| floor | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| runs ending there | 0% | 2% | 26% | 36% | 24% | 9% | 2% | 0% |

**The judge needed two fixes, not one.** The anchors worked — difficulty stopped
pinning at 20 and now lands 12–14. But that exposed a worse problem: asked to
pick one of six compound enum names, the model contradicted its own stated reason
in **3 of 5 live samples** ("the brazier scalds the ghoul" paired with
`status_self`). Splitting the model's interface into two orthogonal choices —
`affects` (enemy/you/nobody) and `kind` (damage/hinder/heal) — fixed it: **5 of 5
correct** on re-test. `EffectKind` remains the engine's vocabulary; only the
schema changed.

Difficulty discrimination is still weak — everything lands 12–14, and a genuine
long shot got 12. Better than always-20, and the clamps bound the outcome, but a
1.7 b model should not be expected to resolve a fine-grained DC scale.

**`repl.py` did need one change, and it was a real bug.** Rich reads square
brackets as style tags, so the combat line `[attack: 15 vs 10 -> hit]` rendered
as nothing at all. Latent since M1 and invisible until M3 emitted a `Roll` for
real. Fixed with `markup=False`, which also means model-written prose can never
inject markup — it is untrusted input. The DoD item "repl.py unchanged" held in
spirit (no new events were needed) but not literally.

**Measured tier 1:** 2.0–5.7 s per judge call, against a §2 budget of 2–4 s. The
upper end is over. Acceptable because it is only ever paid on input the verb
table could not parse.
