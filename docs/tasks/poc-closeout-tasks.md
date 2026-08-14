# POC closeout — bugfixes and one feature, pre-M6

> Raised while closing out the POC (M0–M5, [plan.md](../plan/plan.md) §9), found
> by playing the TUI. Not a new milestone in the §9 sense — three things to fix
> before moving past the vertical slice.

**Goal:** fix two bugs found by playing (TUI movement steals keystrokes;
creative attack verbs get misrouted to `take`) and extend `look` to answer
questions about a specific thing in the room, not just re-describe it.

---

## T1 — TUI: movement must require Enter

**File:** [ui/tui.py](../../src/simulacra/ui/tui.py) — `CommandInput._on_key`, `_MOVE_KEYS`

**The bug.** `_MOVE_KEYS = {"n": "north", "s": "south", "e": "east", "w": "west"}`.
`_on_key` submits immediately — no Enter — the instant a movement key is
pressed on an *empty* input line:

```python
if not self.value and event.key in _MOVE_KEYS:
    event.stop()
    event.prevent_default()
    self.value = _MOVE_KEYS[event.key]
    await self.action_submit()
    return
```

This degrades correctly *mid-word* — `test_a_movement_key_is_a_letter_when_you_are_mid_word`
confirms typing "n" after "ope" appends to make "open", because `self.value`
is non-empty by then. But the **first** keystroke on an empty line is exactly
the ambiguous case: typing "nudge the lever" starts with `self.value == ""`,
so the "n" is swallowed as move-north and submitted before the rest of the
word exists. The player sees "north" happen, and "udge the lever" becomes a
separate, nonsensical next input.

**Fix.** Remove the instant-submit-on-empty-line fast path. Movement typed as
a word ("north" or "n") already works through the normal Enter path —
`Direction.parse` in `parser.py` accepts both — so this shortcut is a
convenience layer that needs to stop auto-submitting, not the only way to
move. Delete the `_MOVE_KEYS` branch from `_on_key` (or gate it behind an
explicit modifier if a single-key shortcut is still wanted), let every
keystroke fall through to `super()._on_key(event)`, and rely on
Enter → `on_input_submitted` → the existing parser path.

Update `tests/test_tui.py`'s `test_a_bare_direction_key_moves_...`-style test
(around L395, `pilot.press("n")` alone moving the player) to assert the new
Enter-required behavior instead.

**Regression test:** typing `n`, `u`, `d`, `g`, `e` with no Enter on an empty
line leaves `cmd.value == "nudge"` and never calls `engine.turn()`; only
Enter submits.

---

## T2 — Parser: creative attack verbs get misrouted to `take`

**Files:** [engine/parser.py](../../src/simulacra/engine/parser.py) (`infer`,
stage 2), [engine/loop.py](../../src/simulacra/engine/loop.py) (`_attack`, `_take`)

**The bug, from a live session:**

```
> karate chop offcut
There is no offcut here.
...
> karate chop offuct
Taken: an unspoiled ration
```

"There is no X here" is `_take`'s message (`loop.py:234`), not `_attack`'s
("There is nothing here to fight", `loop.py:263`) — so despite reading as
combat, these turns are routed to `take`. `offcut` was never actually lost:
it's a hostile `Actor`, and its hp tracks correctly across the whole fight
(10 → 8 → 5). The engine isn't losing the enemy; the parser is misfiling the
verb.

**Root cause.** "karate chop" and "drop kick" aren't in `VERB_ALIASES` (only
`attack`/`hit`/`kill`/`fight` are attack synonyms), so stage 1 (`parse()`)
misses and escalates to stage 2 (`infer()`) — a 48-token, temperature-0
structured call (`Settings.intent`) asking the resident 1.7b model to pick a
verb. It's intermittently classifying flavorful attack phrasing as `take`
instead of `attack`/`improvise`. The typo'd `offuct` case is worse: the
target the model returned appears empty or unrelated, and `_take`'s substring
match (`needle in item.name.lower()`) treats a near-empty needle as matching
anything, silently taking the first item in the room. Note that plain `kick
offcut` (no typo) *did* correctly reach `improvise` in the same session —
this isn't a verb-table gap so much as the 48-token classifier being
unreliable specifically around attack-flavored phrasing, which is worse than
an average misroute because it costs the player a combat turn for free (no
`_resolved`, so no counter-attack that turn) or takes an item they never
asked for.

**Fix, in order of cost:**

1. **Widen `VERB_ALIASES`** with more combat synonyms (`punch`, `kick`,
   `chop`, `strike`, `stab`, `slash`, `swing`). Free, deterministic, and pulls
   the common cases off the unreliable classifier entirely. Won't cover truly
   novel phrasing like "karate chop", but shrinks the surface a lot.
2. **Guard `_take` against a low-confidence target.** It currently takes the
   first substring match unconditionally. When the target came from stage 2
   (`intent.inferred`), consider refusing on an empty/whitespace needle
   outright rather than matching everything. Add a test that `_take` never
   fires on an empty needle regardless of caller.
3. **Bias stage 2 away from `take`/`use` when a hostile is present and no
   item name plausibly overlaps the input.** `improvise` already handles
   "kick offcut" well (judge.py) — it's the safer wrong answer. Consider
   passing the room's actual item/actor names into the stage-2 prompt so the
   model can't confuse an actor for an item, or dropping `take`/`use` from
   the offered enum when the room has no matching item substring.

Start with (1) — cheap, directly fixes the reported transcript. Treat (2)/(3)
as follow-up if misroutes keep happening after.

**Regression test:** `"punch offcut"` / `"kick offcut"` resolve to `attack`
(not `take`) with a hostile named "offcut" in the room; `_take` with an empty
needle refuses rather than taking the first item.

---

## T3 — `look` should support looking at a specific thing

**Files:** [engine/parser.py](../../src/simulacra/engine/parser.py)
(`_INTRANSITIVE`), [engine/loop.py](../../src/simulacra/engine/loop.py)
(`_look`), [narrate/narrator.py](../../src/simulacra/narrate/narrator.py)

**Current behavior.** `look` is in `_INTRANSITIVE` — any trailing target is
discarded, so "look around" and "look at the offcut" behave identically.
`_look()` only re-emits the room description plus the flat contents list
(`Line(f"{actor.name} is here.")`, `Line(f"You see {item.name}.")`). There's
no path to more detail about one specific item, NPC, or monster than that one
line.

**Ask:** `look at <target>` / `examine <target>` should resolve to a specific
item/actor in the room and return generated detail, cached afterward.

**Shape, mirroring `Narrator.room()`:**

- Remove `look` from `_INTRANSITIVE` (no target → today's whole-room
  behavior unchanged; target present → new detail path) — same
  target-resolution shape `_attack`/`_take` already use.
- New `Engine._look_at(target)`: substring-match against `room.items` and
  `room.actors` (reuse the `_attack`/`_take` needle pattern), then:
  - **Item found:** `Item` (`world/model.py`) has no description field today
    — check, and either populate one from the theme/director or generate on
    demand via the narrator, cached like room prose (`prose_cache`, keyed by
    item identity, e.g. `f"item:{item.name}:{digest}"`).
  - **Actor found** (monster or NPC): same pattern — short generated
    description, tier 2, streamed, echo-guarded like `narrator.room()`,
    keyed on the actor.
  - **Not found:** `Notice(f"You don't see {target} here.")`, matching
    `_take`'s miss tone.
- Tier: **tier 2** (~180 tokens, streamed), not tier 1 — looking should feel
  like reading, not like waiting behind a spinner.
- Cache aggressively — looking at the same offcut twice mid-fight should be
  instant; a slow tier-2 call mid-combat is exactly the wrong place to pay
  latency. `prose_cache` already does this for rooms; reuse the pattern.
- Decide whether looking at a *hostile* mid-combat costs a turn
  (`_resolved = True`, provoking a counter-attack) or is free like `_move`'s
  flee case. Free reads better — eyeballing an enemy isn't an action — but
  write the decision down inline, matching the existing `_resolved` comments
  in `loop.py`.

**Regression tests:** `look at <item>` returns detail distinct from the
contents list; `look at <monster>` likewise; repeated look-at is served from
cache (fake client called once); `look` with no target is unchanged
(existing whole-room tests still pass).

---

## Order and dependencies

T1 and T2 are independent and small — either order. T3 is a real feature (new
narrator prompt, new cache key shape, a parser change) and is bigger than
T1+T2 combined; do it last, once T2's target-resolution changes have landed,
since both touch how a typed target gets matched against room contents.

## Definition of done

- [x] Movement in the TUI never submits without Enter; a word starting with
      n/s/e/w typed on an empty line types normally
- [x] `VERB_ALIASES` covers common flavor-attack verbs; `_take` cannot fire on
      an empty/near-empty inferred target
- [x] `look at <target>` / `examine <target>` returns generated, cached
      detail about a specific item or actor
- [x] All new behavior covered by tests with the daemon stopped (`FakeClient`)

## Done

Implemented as scoped. 472 tests green (up from 434), daemon stopped. T2's
guard turned out to need a second layer beyond the task doc's plan: `infer()`
now also refuses to trust a `take`/`use` target that never appeared in the
player's actual input (not just an empty one) — the live repro's "Taken: an
unspoiled ration" came from the model naming a real item it had seen in the
room census, not from an empty needle, so the narrower fix wouldn't have
caught it. `_take`/`_use` also gained a whitespace-only-needle guard as a
second line of defense. T3 added `Narrator.detail()`, cached independently of
room context (kind+name+theme only), and a new `ProseStart` channel
(`"detail"`); no renderer changes were needed since neither `repl.py` nor
`tui.py` branch on channel, only on `speaker`.
