# M12 — Variety · ✅ COMPLETE

> **Status: done.** 810 tests green with the daemon unreachable. Measured live
> on both themes against the M11.1 build; findings at the bottom.

> Scoped from both 2026-09-10 playtests ([first](../playtests/2026-09-10.md),
> [second](../playtests/2026-09-10b.md)). The first showed mirrors on every floor
> of a `simulacra` world; the second showed mirrors, altars and keys in a silver
> mine. Hardpan was the control, and it settled the question: **the repetition
> is the engine's, not the theme's.**

**Goal:** a floor's rooms read as different rooms, and a mine reads as a mine.
Measured, not asserted — word counts before and after, on both themes.

**Constraint:** no new model calls. Every lever here is what goes *into* the
prompts we already make.

---

## The mechanism, as the evidence shows it

Four channels, each amplifying the others:

1. **Room-role words are the engine's, and fantasy-shaped.** The director is
   told "d1r3 is a **shrine**" and the narrator sees `ROLE: shrine`, whatever
   the theme. A small model hears *shrine* and writes altars and statues of gods
   — in a silver mine. `vault` becomes a key, `lair` a void.
2. **Every floor motif goes into every room.** The director's three motifs sit
   on every room's prompt, and it writes them as *objects in particular rooms* —
   so run 2's torch, crumbling wall and hollow altar appeared in every room of
   floor 1, and "a hollow statue in the lair" appeared in the entrance.
3. **The theme's whole motif list goes into every room too**, through the
   narrator's system prompt — which is how `tallow` reached 24 mentions and
   `half-remembered` became simulacra's signature phrase.
4. **The director's names converge, and overwrite the theme's.** "Chamber of
   Shadows", "Lair of the Forgotten", "Descent into the Unknown" appear in both
   themes' worlds; floor names repeated within a world ("The Hollowed Depths" on
   every floor) despite the do-not-repeat list.

And the director's text can carry **room ids** into prose — *"a rusted iron key
lies in the wall of d1r0"* — because its census names rooms by id.

---

## V1 — Roles in the theme's words

**Files:** [world/theme.py](../../src/simulacra/world/theme.py),
[world/director.py](../../src/simulacra/world/director.py),
[narrate/narrator.py](../../src/simulacra/narrate/narrator.py)

`RoomKind` stays structure. What the *model* is told is a role description: the
theme's, from an optional `[roles]` table, or a setting-neutral engine default —
*"the floor's most remarkable place"* rather than *shrine*,
*"where something worth having is kept"* rather than *vault*. The neutral
defaults are the fix; a theme's own roles are enrichment.

## V2 — One mood per room, and moods are not objects

**Files:** `director.py`, `narrator.py`

- The director's motifs become **mood words**: at most three words each, no room
  ids, validated in code — a sentence about an object in a room is dropped.
- Each room's prompt carries **one** mood, rotated across the floor's motifs, as
  `MOOD:` — never the whole list.
- The theme's motif list leaves the per-room system prompt; it is the fallback
  mood source when the director gave none.
- The prompt shape changes, so `PROSE_VERSION` moves again.

## V3 — The director keeps the theme's names, and stops repeating itself

**Files:** `director.py`, [engine/loop.py](../../src/simulacra/engine/loop.py),
[memory/store.py](../../src/simulacra/memory/store.py)

- A director room name built on a stock abstraction (*forgotten, unknown, lost,
  shadows, void, oblivion…*) or headed by a generic room word (*chamber of…,
  lair of…, descent into…*) is refused; the theme's own pool name stays.
- A floor name already used in the world is refused, and the floor takes the
  theme's `floor_name` pattern instead.
- The avoid-list carries previous floors' **motifs** as well as names, and a
  repeated motif is dropped in code — the model has shown it will not honour
  the list on its own.
- Room ids are stripped from anything the director writes.

## V4 — A described thing you can't take says so

**Files:** `engine/loop.py`

*"A rusted iron key lies in the wall"* → `take key` → *"There is no key here."*
The prose and the engine contradict each other. When the target is something the
room's own text mentions, say it's part of the room.

## V5 — Measure

Fresh worlds with fixed seeds, both themes, floors 1–3, the real model — once on
the M11.1 build and once on M12:

| Metric | Why |
|---|---|
| share of a floor's rooms containing each of its motif words | the stamping rate — before, ~all of them |
| `mirror`, `altar`, `shrine`, `key` in Hardpan prose | the cross-theme leak |
| director room names using stock abstractions | the convergence |
| floor names repeated within a world | the ignored avoid-list |
| distinct content words per floor | variety, as one number |

## Definition of done

- [x] No engine role word reaches a prompt
- [x] No room prompt carries more than one mood
- [x] No room id reaches prose
- [x] No floor name repeats within a world
- [x] Hardpan prose stops describing altars and mirrors — measured
- [x] `take` on a described fixture explains itself

---

## Findings

### V5 — measured

Fresh worlds with fixed seeds (simulacra 12001, Hardpan 12002), floors 1–3,
every room narrated by `qwen3:1.7b`, once on the M11.1 build and once on M12.
The metrics live in the comparison script rather than in either build, so both
are judged the same way. Hardpan ran on the engine's **neutral role defaults
alone** — its theme pack was not edited — which makes it the test of whether
the engine was the cause.

| | Hardpan before | after | simulacra before | after |
|---|---|---|---|---|
| **words in half a floor's rooms** | **64** | **21** | **31** | **8** |
| `mirror` per 1k words | 9.1 | **1.1** | 10.4 | 5.6 |
| `glass` / `shattered` per 1k | 5.1 / 4.5 | **0 / 0** | 2.3 / 3.5 | 3.7 / 0 |
| `altar`, `shrine`, `statue` per 1k | 0.6 each | **0** | 2.3 / 0 / 0 | 0 |
| director room names that are stock phrases | 4 of 5 | **0 of 14** | 8 of 8 | **0 of 10** |
| floor names repeated in the world | 2 | 1 † | 2 | **0** |
| distinct content words per floor | 0.66 | 0.72 | 0.68 | **0.82** |

"Words in half a floor's rooms" is the playtests' complaint as one number —
*the torch is in every room*. Before, `mirror` was in at least half the rooms
of **all three** Hardpan floors, and `shattered` and `broken` of two; after,
what's left at that spread is the model's own verbal tics (`thick`, `scent`,
`something`), which M12 doesn't target.

† Hardpan's one remaining "repeat" is the metric comparing the engine's own
fallback titles, *"Floor 1"* and *"Floor 3"*, by stem. Two of its three
director floor names were refused and took that title, because `hardpan.toml`
has no `floor_name` pattern; simulacra's *"The {nth} Impression"* would read
far better there.

**Mirrors in simulacra halved and did not vanish.** That is the theme's own
premise — a copy of somewhere — and the director named its second floor
*"The Shattered Mirror"* itself. What changed is that `mirror` no longer sits
in half of any floor's rooms.

**Keys and room ids were not reproduced** by these seeds on either build, so
this measurement doesn't speak to them; the unit tests do.

### The director dodges avoid-lists by numbering

The pre-M12 baseline named one world's floors *"Erebus's Veil"*, *"Erebus's
Veil II"* and *"Erebus's Veil III"*: told not to repeat a name, the model
appended a numeral. M12's first cut compared names exactly and would have let
that through. Floor names are now compared by stem — articles and trailing
numerals stripped. Found in the baseline data before the M12 measurement
finished; the M12 run produced no numbered names, so its numbers stand for the
final code.

### Prose got shorter, and closer to the brief

Words per room fell from about 104 to about 55 on Hardpan, and from about 51 to
about 32 on simulacra. Both theme briefs ask for two sentences; plan.md §11 has
recorded the model overshooting that since M2. Less to riff on — one mood, no
motif list, a role described rather than named — reads as less to pad with.
The per-1,000-word rates above keep the comparison fair despite it.

### Old worlds needed the rule on the way out, too

Found writing the report, after the first commit. The mood rule ran on the
director's output, so it protected every floor directed from M12 on — but a
world made before M12 stores its motifs as objects in particular rooms, and they
came back from the graph unfiltered. The narrator now applies the same rule when
it reads them, falling back to the theme's own moods when nothing survives.
Without it, the Hardpan world being played at the time would still have put *"a
hollow statue in the lair"* into a room that wasn't the lair.

### What M12 does not fix

- **The model's own tics.** `something`, `thick`, `scent`, `forgotten` still
  lead the frequency list in both themes. They are the model's, not a prompt's.
- **A refused floor name costs identity.** Two of Hardpan's three floors are
  *"Floor N"*. Refusal is the right call when the alternative is a third
  *"Echoes of the Depths"*, but a theme `floor_name` pattern is what makes the
  fallback worth reading.
