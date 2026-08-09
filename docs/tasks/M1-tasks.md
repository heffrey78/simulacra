# M1 — Walk around, no LLM

> Milestone from [plan.md](../plan/plan.md) §9. **Status: ✅ complete.**
> All six tasks done, 214 tests green with the Ollama daemon unreachable.
> One defect surfaced by playtesting and since fixed — see
> "loops short-circuit the spine" in plan.md §11.

**Goal:** a playable loop with **zero model involvement**. You start in the
entrance, walk the floor, pick things up, take stairs down, and quit. Nothing
streams, nothing blocks, nothing needs the Ollama daemon.

**Why this shape:** M1 exists to prove the *event seam* (plan.md §6) before any
LLM code depends on it. If the engine/renderer boundary is wrong, it is far
cheaper to find out now than after the narrator and prefetcher are built on top.
A secondary payoff: the entire milestone is testable in CI with no daemon, so M1
becomes the regression floor for everything after it.

**Non-goals — explicitly deferred, do not build these here:**

| Deferred | Milestone |
|---|---|
| Any LLM call whatsoever | M2+ |
| Streamed prose (`ProseStart`/`ProseDelta`/`ProseEnd`) | M2 |
| Parser stage 2 (intent inference) | M3 |
| Combat, judge, death | M3 |
| Writing memories / embeddings | M4 |

---

## Already done — do not rebuild

- [floorgen.py](../../src/simulacra/world/floorgen.py) — floors generate, 77 tests green
- [theme.py](../../src/simulacra/world/theme.py) + [simulacra.toml](../../themes/simulacra.toml)
- [model.py](../../src/simulacra/world/model.py) — including `Direction.parse()` with `n/s/e/w/u/d` aliases
- [events.py](../../src/simulacra/engine/events.py) — full event vocabulary
- [store.py](../../src/simulacra/memory/store.py) — smoke-tested
- `GameState` in [state.py](../../src/simulacra/engine/state.py) is already a complete dataclass; T2 only adds a constructor helper

---

## T1 — Parser stage 1

**File:** [engine/parser.py](../../src/simulacra/engine/parser.py) · implement `parse()`

A pure function. No state, no I/O, no model. Returns `Intent | None`.

**Details that matter:**

- **Bare directions must work without a verb.** `n`, `north` are the single most
  common input in the game; requiring `go north` would be a usability failure.
  Check `Direction.parse()` on the whole input *before* the verb table.
- Normalise first: lowercase, strip, collapse internal whitespace.
- **Match multi-word verbs before single-word ones** — `pick up lamp` must not
  parse as verb `pick`. Sort the alias table by token count descending.
- Strip leading articles (`the`, `a`, `an`) from the target, so `take the lamp`
  and `take lamp` produce an identical `Intent`.
- **Return `None` on no match — never raise.** `None` is the signal the caller
  uses to escalate to stage 2 in M3. Raising would make that escalation a
  try/except, which is the wrong shape.
- Set `Intent.raw` to the normalised input always; M3's stage 2 needs it.

**Alias table** (starting point, extend freely):

| Verb | Aliases |
|---|---|
| `move` | `go`, `walk`, `head`, `move` + bare directions |
| `look` | `look`, `l`, `examine`, `x`, `inspect` |
| `take` | `take`, `get`, `grab`, `pick up` |
| `inventory` | `inventory`, `inv`, `i` |
| `attack` | `attack`, `hit`, `kill`, `fight` |
| `talk` | `talk`, `speak`, `ask`, `talk to` |
| `descend` | `descend`, `stairs` |
| `wait` | `wait`, `z` |
| `quit` | `quit`, `exit`, `q` |

**Decision to record —** `down` is ambiguous: a direction *and* the intent to
descend. Resolve it as `move(DOWN)` in the parser, and let the **engine** treat
arriving in a `RoomKind.DESCENT` room via `DOWN` as a descent. Keeping the parser
context-free is what keeps it a pure function.

**Acceptance:** table-driven tests cover every alias, bare directions, article
stripping, empty/whitespace input (→ `None`), and unknown verbs (→ `None`).

---

## T2 — Run bootstrap

**File:** [engine/state.py](../../src/simulacra/engine/state.py) · add
`new_run(store, theme, settings, seed=None) -> GameState`

**Details that matter:**

- `store.start_run(theme.name)` → `run_id`, then `generate_floor(1, theme, rng)`.
- **Seed the RNG explicitly** from `--seed`, falling back to a random seed that
  gets *recorded* in the run row. A floor you can't reproduce is a bug you can't
  reproduce.
- **Persist the floor into the graph now, even though nothing reads it until M4.**
  This is the one piece of M4 work worth pulling forward: it is ~10 lines here
  and a painful backfill later, because by M4 the interesting runs will already
  have happened.

  ```
  upsert_node(f"room:{r.id}", "room", r.name, run_id=run_id)
  link(f"room:{a}", f"EXIT_{direction}", f"room:{b}", run_id=run_id)
  link(f"run:{run_id}", "ENTERED", f"floor:{depth}", run_id=run_id)
  ```

  Call `store.commit()` once at the end, not per node.

**Acceptance:** two `new_run` calls with the same seed produce identical floors;
the run row exists; room nodes and exit edges are queryable via `store.neighbors()`.

---

## T3 — Engine loop

**File:** [engine/loop.py](../../src/simulacra/engine/loop.py) · implement
`begin()`, `turn()`, `descend()`

**First, reshape the constructor.** It currently takes seven positional
collaborators, most of which don't exist until M2:

```python
def __init__(self, state, store, settings, *,
             narrator=None, prefetcher=None, client=None): ...
```

M1 constructs `Engine(state, store, settings)` and nothing else.

**Details that matter:**

- **Emit cheap, certain events before expensive ones.** `RoomEntered` and
  `StatusChanged` go out before any description. This costs nothing now and is
  what lets a future TUI update its panels while prose streams behind them.
- **Put the room description behind one `_describe(room)` helper.** In M1 it
  yields `Line` events. In M2 it yields `ProseStart`/`ProseDelta`/`ProseEnd` from
  the narrator. **If the swap touches anything outside that helper, the seam is
  wrong.** This is the single most important structural detail in M1.
- `turn()` increments `state.turns` exactly once per call, including on invalid
  input — a rejected command is still a turn for the purposes of M3's monsters.
- Set `room.visited = True` on entry and pass `first_visit` correctly on
  `RoomEntered`; M2 keys prose caching off it.
- **The engine must never import from `simulacra.ui`.** T6 enforces this with a test.

**Verb handling in M1:**

| Verb | Behaviour |
|---|---|
| `move` | validate against `room.exits`; on failure `Notice("You can't go that way.")` and no room change |
| `look` | re-emit `RoomEntered` + description for the current room |
| `take` | case-insensitive **substring** match against `room.items` (players type `take water`, not the full name); move to inventory, emit `ItemTaken`; `Notice` if absent |
| `inventory` | `Line` listing, or "You carry nothing." |
| `descend` | only from `RoomKind.DESCENT`, else `Notice`; generate next floor, emit `FloorDescended` |
| `wait` | turn passes, `Notice` |
| `quit` | `RunEnded(cause="quit")` |
| `attack`, `talk` | `Notice("Not yet.")` — stubs land in M3 |
| unparsed | `Notice("I don't understand.")` — stage 2 arrives in M3 |

- `descend()` in M1 calls `generate_floor` directly with **no director call** and
  emits no `Thinking` event. Both arrive in M2.

**Acceptance:** a scripted sequence of inputs drives a full traversal from
entrance to descent and down one floor, with no model running.

---

## T4 — REPL renderer

**File:** [ui/repl.py](../../src/simulacra/ui/repl.py) · implement
`ReplRenderer.handle()` and `read_input()`

The only module allowed to know about terminals.

**Details that matter:**

- **Ignore unrecognised events silently.** M1 will never see `Roll` or
  `ProseDelta`; handling-by-lookup with a default of "do nothing" means M2 and M3
  add renderer cases without ever breaking the loop mid-milestone.
- **`ProseDelta` must print with `end=""` and flush**, even though nothing emits
  it until M2. Write it now while the reason is fresh — this one line is what
  makes streaming feel instant rather than arriving in chunks.
- Render `RoomEntered` exits in a **stable display order** (N/E/S/W/U/D), not
  `dict` insertion order, which is generation order and reads as random.
- `read_input()` must treat EOF (Ctrl-D) and `KeyboardInterrupt` as `"quit"`, not
  as a traceback.
- Map `Line.style` → rich styles: `title` bold, `dim` dim, `alert` red,
  `good` green, `normal` default.

**Acceptance:** manual play is legible; a `FakeRenderer` in tests collects events
without touching a terminal.

---

## T5 — Wire up `__main__`

**File:** [__main__.py](../../src/simulacra/__main__.py) · replace the `SystemExit`

Argument parsing already exists.

**Details that matter:**

- **Do not construct an `OllamaClient` in M1.** No LLM means `simulacra` must run
  with the daemon stopped — that is a feature, and a test of it.
- Order: `Settings.load()` → `Theme.load()` → `Store()` → `new_run()` →
  `Engine` → `ReplRenderer`.
- Drive `begin()` through the renderer, then loop: `read_input()` → `turn()` →
  render each event → break on `RunEnded`.
- **Always `store.end_run(...)` and close the store**, including on exception —
  use `try/finally`. An orphaned open run row will confuse M4's `previous_runs()`.

**Acceptance:** `simulacra --theme simulacra --seed 42` is playable start to
finish with `systemctl stop ollama` (or equivalent) in effect.

---

## T6 — Tests

**Files:** `tests/test_parser.py`, `tests/test_engine_m1.py`, `tests/test_seam.py`

- **`test_parser.py`** — table-driven over the alias table, bare directions,
  articles, empty input, unknown verbs.
- **`test_engine_m1.py`** — a `FakeRenderer` collecting events into a list. Drive
  a seeded floor from entrance to descent; assert event *types and order*, not
  prose text. Cover: invalid direction produces `Notice` and no `RoomEntered`;
  `take` moves the item; `descend` from a non-descent room is refused; turn
  counter increments on rejected input.
- **`test_seam.py`** — assert no module under `engine/`, `world/` or `memory/`
  imports `simulacra.ui`. Cheap to write, and it is the only thing that will
  actually stop the seam eroding under time pressure.

**Acceptance:** `pytest` green with the Ollama daemon **stopped**.

---

## Order and dependencies

```
T1 parser ─┐
T2 bootstrap ─┼─> T3 engine ──> T5 __main__
T4 renderer ─┘                     │
T6 tests ───────────────────────────┘  (write alongside, not after)
```

T1, T2 and T4 are independent and can be done in any order. T3 needs all three.

## Definition of done

- [x] `pytest` green with Ollama stopped
- [x] `simulacra --seed 42` playable: walk, look, take, inventory, descend, quit
- [x] Same seed produces the same floor across runs
- [x] Room nodes and exit edges present in the DB after a run
- [x] Engine imports nothing from `ui`; description confined to `_describe()`
- [x] Run row closed with a cause on every exit path

## Watch for

**The temptation to "just add" narration.** M1 with placeholder `Line` output
will feel flat, and the fix is one call away. Resist it until T6 is green — M2 is
where the latency budget gets tested, and it needs a known-good, fully-tested
loop underneath it to be a fair test.

**`_describe()` growing a second caller.** If anything other than `begin()`,
`look` and movement produces room text, the M2 swap stops being a one-function
change.
