# M5 — Textual TUI

> Milestone from [plan.md](../plan/plan.md) §9. Estimated one to two days.

**Goal:** a panelled terminal UI — prose log, status, fog-of-war map, input line —
built as a *second renderer*, not a rewrite.

**Why this shape:** this is the milestone the event seam was designed for. plan.md
§6 has said since M1 that a Textual frontend is "a sibling of `repl.py`
implementing the same protocol". M2 proved the seam holds when `_describe()`
swapped from flat `Line`s to streamed prose without touching anything else. M5 is
the claim being cashed: **if this milestone needs to change the engine, the seam
was a fiction.** One event-vocabulary addition is expected (T3); anything beyond
that is a finding worth writing down.

**Non-goals:**

| Deferred | Why |
|---|---|
| Replacing the REPL | It stays. It is the scriptable, pipeable, CI-friendly frontend. |
| Mouse support, themes, animations | Cost without benefit on this box — see T7. |
| Changing any engine behaviour | If you need to, stop and reconsider. |
| Tool calling, richer graph queries | Separate M5 items. |

---

## Already done — do not rebuild

- **The seam.** `Renderer` in
  [events.py](../../src/simulacra/engine/events.py) is a one-method protocol.
  `MemoryWriter` already proves a second consumer works — `__main__` fans every
  event out to both it and the renderer.
- **Event ordering is already TUI-shaped.** `RoomEntered` and `StatusChanged` are
  emitted *before* prose specifically so panels can repaint while text streams
  behind them. That was speculative in M1; M5 is what makes it pay.
- **`test_seam.py`** already forbids `engine/`, `world/` and `memory/` from
  importing `ui` or calling `print()`. It will keep the TUI honest for free.
- **Everything the panels need is already in events**: `StatusChanged` (hp,
  max_hp, depth, effects), `RoomEntered` (name, exits, first_visit),
  `FloorDescended` (depth, theme_name, goal), `Thinking` (spinner label),
  `NpcPresent` (remembers), `Roll`/`Damage`, `RunEnded` (cause, epitaph).
- **Textual is verified on this box**: 8.2.8 installs on CPython 3.14 and
  `App.run_test()` drives it headless, so the TUI is CI-testable like everything
  else.

---

## The decision to make first

**The engine is a synchronous generator. Textual is async. This is the whole
problem.**

`engine.turn()` blocks — a descent runs the tier-3 director for ~18 s, and even a
routine room narration is ~3.5 s. Calling it on Textual's event loop freezes the
entire UI for that long: no spinner, no repaint, no keystrokes. The `Thinking`
event would be posted and then never rendered, because the thread that would
render it is the thread that is blocked.

So: **run the engine in a Textual worker thread and post each event to the UI as
it is yielded.**

```python
@work(thread=True, exclusive=True)
def run_turn(self, text: str) -> None:
    for event in self.engine.turn(text):
        self.call_from_thread(self.dispatch, event)
```

Two consequences to design around, not discover:

- **Per-event posting is what makes streaming work.** `ProseDelta` already
  arrives one chunk at a time; post each one and the text appears as it
  generates, which is the entire M2 thesis (15 tok/s outruns reading at 4 wps).
  Collecting the generator into a list first would silently undo M2.
- **`GameState` is being mutated on the worker thread.** Do not read it from the
  UI thread for panel contents. Everything a panel shows must come from the
  event it was handed. T3 exists because the map is the one place that rule bites.

---

## T1 — Dependency and entry point

**Files:** [pyproject.toml](../../pyproject.toml),
[__main__.py](../../src/simulacra/__main__.py)

- Add Textual as an **optional extra**, not a core dependency:
  `[project.optional-dependencies] tui = ["textual>=8.2"]`. notes.md's first line
  is "must use minimal resources"; the headless/offline path should not grow a UI
  framework it never imports.
- `--ui {repl,tui}`, defaulting to `repl`. Import Textual lazily inside the `tui`
  branch so a missing extra produces "install with `pip install -e '.[tui]'`"
  rather than an ImportError at startup.
- **Everything before the frontend choice stays shared** — settings, theme,
  store, client warmup, `new_run`, `Engine`, `MemoryWriter`. Only the renderer
  and the input loop differ. If the two branches diverge further than that,
  factor the setup into one function rather than duplicating it.

---

## T2 — The app shell and the worker

**File:** new `src/simulacra/ui/tui.py`

Layout:

```
┌────────────────────────────────────────┬──────────────────┐
│ prose log (scrolling, focus of the     │ status           │
│ screen — this is where the game is)    │  hp ▓▓▓▓░░ 14/23 │
│                                        │  floor 4         │
│                                        │  braced (2)      │
│                                        ├──────────────────┤
│                                        │ map (fog of war) │
│                                        │                  │
├────────────────────────────────────────┴──────────────────┤
│ > _                                                       │
└───────────────────────────────────────────────────────────┘
```

**Details that matter:**

- `RichLog` for prose. **Append `ProseDelta` without a newline** and only break
  on `ProseEnd` — the same rule `repl.py` follows with `end=""`. Getting this
  wrong turns streamed prose into one word per line.
- **Disable the input while a turn is in flight.** The engine is single-threaded
  and `OllamaClient` serializes requests; queued turns would pile up behind a
  20 s director call. `exclusive=True` on the worker plus a disabled `Input` is
  the honest representation of what the engine can actually do.
- `Thinking` shows a spinner; **clear it on the next event of any kind**, not on
  a timer. The engine tells you when it is done by continuing.
- Keep `markup=False` when writing model text into `RichLog`. Rich reads square
  brackets as style tags — this already ate the combat line once in M3, and
  model output is untrusted input.

---

## T3 — The map, and the one event change

**File:** `src/simulacra/ui/tui.py`

The map is built **from events**, not from `state.floor`. That avoids the
worker-thread race entirely and gives fog of war for free: you draw what you have
seen, because that is literally all you were told.

**But `RoomEntered` does not say how you arrived**, and a grid layout needs the
direction travelled. This is the one place the existing vocabulary genuinely
cannot express what a renderer needs.

**Add `via: str | None` to `RoomEntered`** — the direction moved, `None` for
`begin()`, `descend()` and `look`. It is a renderer-facing fact the engine
already knows and currently throws away.

- Do this as a **field with a default**, so every existing construction site and
  test keeps working.
- Update `_move` to pass it; leave `_look`, `begin` and `descend` at `None`.
- Add an engine test that `via` matches the direction travelled. The map is
  wrong in a way nobody notices otherwise.

Layout: assign the first room `(0, 0)`; on each `RoomEntered` with a `via`, offset
from the previous room's coordinate. Unexplored exits render as stubs. Reset the
grid on `FloorDescended`.

---

## T4 — Status panel

- Driven entirely by `StatusChanged`, which already carries hp, max_hp, depth and
  effects. Nothing else should feed it.
- An hp bar that changes colour under ~⅓ is worth the ten lines; the REPL cannot
  do this and it is most of why a TUI is nicer.
- `FloorDescended` sets the floor name and goal in the header — currently the
  goal scrolls away in the REPL after one line, which wastes the tier-3 call that
  produced it.

---

## T5 — Input and keybindings

- `Input` submitted on Enter, cleared, echoed into the log so the transcript
  reads as a conversation.
- Command history on ↑/↓. Cheap, and the REPL has never had it.
- Bindings: `ctrl+c` quit, `ctrl+l` clear log, and single-key movement (`n`/`s`/
  `e`/`w`) **only when the input is empty** — otherwise you cannot type "north".
- Route everything through `engine.turn(text)`. **No bypassing the parser**: a
  keybinding must submit the same string the player could have typed, or the TUI
  and REPL diverge in behaviour and stage-2 inference stops being exercised.

---

## T6 — Tests

**File:** `tests/test_tui.py`, marked so a missing extra skips rather than fails.

`App.run_test()` is verified working headless on this box.

- Prose deltas accumulate into one paragraph, not one line each.
- Input is disabled while a turn runs and re-enabled after.
- `Thinking` shows a spinner; the next event clears it.
- Status panel reflects the last `StatusChanged` only.
- Map: three moves produce three placed rooms in the right relative positions;
  `FloorDescended` resets it.
- Unknown events are ignored silently (same rule as `repl.py` — this is what lets
  a future milestone add an event without breaking the UI mid-flight).
- **`test_seam.py` still passes**, unchanged.

---

## T7 — Measure the cost on *this* box

**This is not optional polish, it is the milestone's real risk.**

An 8-core CPU with no usable GPU is running inference; a repainting TUI competes
for exactly those cores. A prefetch that gets slower because the UI is animating
is a direct regression against M2's measured budget.

- Run [doctor](../../src/simulacra/llm/doctor.py) with the TUI open and idle, and
  compare tok/s against the REPL baseline (tier 2 ≈ 3.5 s, tier 3 ≈ 18 s).
- **Coalesce `ProseDelta` repaints.** Tokens arrive ~15/s; repainting per token
  is 15 full-widget refreshes a second for no perceptible gain over ~5. Batch
  deltas on a short timer if the measurement says it matters.
- No animations, no auto-refresh timers, no background CSS transitions.
- Record the numbers in plan.md §9 like every other milestone.

---

## Order and dependencies

```
T1 entry ──> T2 shell+worker ──> T4 status ──> T6 tests
                    │              T5 input ──┘
                    └──> T3 map (+ the RoomEntered change)
                                   T7 measure ── last, on the finished UI
```

## Definition of done

- [x] `pytest` green with Ollama stopped, TUI tests included — **434 passing**
      (402 before M5, +2 engine tests for `via`, +30 TUI). With the extra
      uninstalled: 404 passing, 1 skipped.
- [x] `simulacra --ui tui` plays a full run: walk, fight, talk, descend, die —
      verified on two live runs against `qwen3:1.7b`. Run 2: the Archivist
      answers (*"127. The delver's number is 127… the ledger remains silent."*),
      a fight takes the bar 20 → 16 → 14, a descent lands on floor 2 behind a
      29.6 s spinner, and *a partial* finishes the delver at 0/23 with an
      epitaph.
- [x] `simulacra` (REPL) is byte-for-byte unaffected — `repl.py` is untouched;
      `__main__` moved shared setup into a `Session` both frontends build,
      leaving the REPL loop itself the same reads and the same fan-out.
- [x] Prose streams token by token; the UI stays responsive during an 18 s descent
- [x] Map shows only visited rooms and resets per floor
- [x] `test_seam.py` unchanged and passing
- [x] Exactly one event-vocabulary change (`RoomEntered.via`), justified in the doc
- [x] tok/s with the TUI open measured against the REPL baseline — **15.9 vs 15.6**

## What actually happened

**The seam held.** One event field, `RoomEntered.via`, exactly as budgeted. No
other file under `engine/`, `world/` or `memory/` changed. `test_seam.py` was
not touched and did not need to be.

**The seam does not cover concurrency, and that is the honest finding.** The
event protocol says nothing about *who is on which thread*, so the whole
sync-generator-versus-async-UI problem lands on the renderer. That is the right
place for it — the fix was entirely inside `tui.py` — but it is the one thing a
second frontend could not simply inherit from the first.

**`RichLog` cannot stream.** It renders each `write()` straight to fixed `Strip`s,
so there is no way to append to a partial line — which is precisely what
"append `ProseDelta` without a newline" requires. The log is therefore a
`RichLog` for committed text plus one `Static` beneath it holding the in-flight
paragraph, which is committed to the log on `ProseEnd`. Same visible result, and
the T6 test asserting three deltas do not become three lines is what guards it.

**Floors are not planar, so the map cannot be.** `floorgen` picks each
connection's direction at random and adds loop edges between arbitrary spine
rooms, so two rooms genuinely dead-reckon onto the same square. One square holds
one room: the newer sighting wins and the older is forgotten, which is what
happens on a hand-drawn map. This is a property of the dungeon, not a bug in the
renderer, and it has its own test.

**A panel is not a scrolling line, and that found a bug.** T4 says the status
panel is driven "entirely by `StatusChanged`". Combat does not emit one — the
engine only sends `StatusChanged` when effects tick, an item is used or a room is
entered — so through the whole of the first live fight the bar sat at **23/23**,
and it was still reading 23/23 when the log said the killing blow landed. In the
REPL this is invisible, because a status *line* that is out of date has already
scrolled away.

The fix stays inside the discipline: `Damage.hp_left` is already in the stream
and for `target == "you"` it is exactly the player's hp. No engine change, no
state read, one extra event feeding the panel a number it was handed. Verified on
the second live run: 20 → 16 → 14 → 0, matching `player.hp` at every step.

**The measurement changed the code once.** The `Input` cursor blink was the only
thing in the UI repainting at idle and cost 5× everything else put together
(1.00% → 0.20% of one core with it off) — stolen directly from the prefetcher.
T7 was not polish.

**Known gap, deliberately not fixed:** floor 1 has no goal in the status header.
`begin()` does not emit `FloorDescended` — only `descend()` does — so the tier-3
goal for the first floor is never in the event stream. Reaching into
`state.floor.goal` for it is exactly the race this design forbids, and emitting
`FloorDescended` from `begin()` would change what the REPL prints. It wants a
new event, decided deliberately, not a shortcut.

## Watch for

**Collecting the generator.** `list(engine.turn(text))` before rendering is the
single easiest way to silently destroy M2. It will look fine — the text still
appears — but it will appear all at once after a 6 s wait instead of streaming.

**Reading `state` from the UI thread.** It works right up until it doesn't, and
the failure is a torn read during a descent, which is exactly when you are least
able to reproduce it.

**The TUI becoming the reason to change the engine.** Every previous milestone
held this line. If a panel wants something the events don't carry, the answer is
a new event or a new field — decided deliberately, like T3 — not a reach into
game state.

**Scope.** A map, a status bar and a log is a finished TUI. Inventory management
screens, mouse targeting and a settings panel are a different project.
