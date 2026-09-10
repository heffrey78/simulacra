# M11 — Playtest fixes · ✅ COMPLETE

> **Status: done.** 732 tests green with the daemon unreachable. Verified by
> replaying the playtest against the world it was played in — which found one
> more bug, dating from M10. Findings at the bottom.

> Scoped from the [2026-09-10 playtest](../playtests/2026-09-10.md), the first
> extended play after M10. Every task here traces to something seen in that
> session or in the world file it left behind.

**Goal:** replay the playtest's own inputs and have every question in it answer
itself. A taken jar is gone from the description; `search` finds something the
room didn't already say; "braced" means something and says what; the session can
be read back afterwards.

**Constraint:** tier 0 wherever possible. The playtest's clearest pattern was that
the tier-1 parser fallback sat behind almost every problem — `search`, `hide`,
`jump`, `enter`, `equip` — each costing a model call and each routed by a small
model with a documented habit of lifting targets from the room. **Most of this
milestone is removing model calls, not adding them.**

---

## Non-goals

| Deferred | Where |
|---|---|
| Room-description variety (the mirror problem) | M12 — needs before/after word counts, not just tests |
| Canon holding contradictory facts | design note — the playtest decided *hold both* |
| Looting, corpses, drops | own milestone — every drop moves M3's balance curve |
| Weapons with tradeoffs | later — until they differ in more than damage, choosing is not a decision |

---

## P1 — The description stops listing things that can leave

**Files:** [narrate/narrator.py](../../src/simulacra/narrate/narrator.py)

The room prompt includes `CONTAINS:` and `PRESENT:`, so the narrator writes the
jar into the prose — *"The jar of clean water sits beside the door, untouched"* —
and the prose is cached on a key of theme, concept and floor name, which ignores
items. Take the jar and the description still has it, forever; since M6 the
cache survives across runs, so it has it on run 20 too. Same bug for a dead
monster, and since M9 for an NPC who followed you out.

Take items and actors out of the prose prompt. `_contents` already lists them
unambiguously on every visit, which was M2's stated intent — "the narrator may
mention these in passing, but the player still needs an unambiguous list".
Caching turned *in passing* into *permanently*.

**The cache key has to change with the prompt.** Existing worlds hold prose
generated with items in it, under keys that would still match. Salt the key with
a prompt version so those rooms regenerate once.

**Tests:** the census has no items or actors; the key differs from the M10 key;
taking an item and re-describing the room cannot surface it.

---

## P2 — `search` is a verb

**Files:** [engine/parser.py](../../src/simulacra/engine/parser.py),
[engine/loop.py](../../src/simulacra/engine/loop.py)

Not being one cost a tier-1 call per search and let the fallback pick the target.
In the playtest's world file, **all ten discoveries were keyed on the room's own
name**: bare `search` in *the narrowing* became `look at narrowing`, and M10 then
counted the room's name as a mentioned noun, generated a second description of
the whole room, and filed it as a find.

- **Bare `search`** looks for what the room *hasn't* said: an undiscovered
  fixture from the theme's pool, if the room is fertile and has budget. Nothing
  left is "You find nothing more here", and costs nothing.
- **`search <thing>`** is the M10 discovery path, unchanged.
- **The room itself is never a discovery.** A target whose words are all in the
  room's name replays the room's prose, free.
- `rummage` and `loot` alias it. `loot` gets an honest answer about corpses —
  they don't persist — rather than a search of the walls.

**Tests:** bare `search` makes no parser call; it finds a fixture in a fertile
room and nothing in a barren one; `search the threshold` in the Threshold writes
no canon and costs no call.

---

## P3 — The fallback stops inventing targets

**Files:** `engine/parser.py`

`infer` already refuses a model-invented target for `take` and `use` — M3 found
"karate chop offuct" becoming `take ration`, lifted from the census. The same
lifting drove P2's bug through `look`. Extend the guard to `look` and `search`:
a target not in the player's own words is dropped, not trusted.

**Tests:** a `look` whose inferred target is absent from the input loses it.

---

## P4 — Statuses mean what they say, and do it

**Files:** [engine/judge.py](../../src/simulacra/engine/judge.py),
[engine/combat.py](../../src/simulacra/engine/combat.py), `engine/loop.py`

"Braced" was three problems at once:

- **Backwards.** The judge's (you, hinder) pair means *this hurt your position*;
  the engine labelled every one of them "braced", which reads as a buff.
- **Inert.** Nothing in the engine reads it. It counted down and displayed.
- **Doubled.** The verdict and the effect tick each emitted a status line in the
  same turn.

Two statuses, both read by combat:

| Status | Source | Effect |
|---|---|---|
| `shaken` | judge, (you, hinder) | −2 defense while it lasts |
| `hidden` | the new `hide` verb | the next monster round skips you; your next attack gets +2 and ends it; cannot hide two turns running |

One status line per turn: the tick reports only when something *expires*.

**And the judge stops describing things that did not happen.** *"Jumping over
the edge causes the enemy to stumble"* was ruled a hit in a room with no enemy,
and the effect silently fell through. The room summary now says when nothing
hostile is present, and a verdict aimed at an absent target says so instead of
rolling.

**Tests:** `shaken` lowers the defense monsters roll against; a `hidden` player
is not attacked; hiding twice running is refused; one status line per turn; an
enemy-targeted verdict with no enemy rolls nothing.

---

## P5 — The verbs players reached for

**Files:** `engine/parser.py`, `engine/loop.py`

| Verb | Does | Model call |
|---|---|---|
| `hide` | P4 | none |
| `equip`, `wield` | says what is in hand, and that the best weapon is always used | none |
| `enter <place>` | a neighbouring room by name → go there; the stairs → descend; else look at it | none |

`equip` is honest rather than a mechanic: combat always uses the highest-damage
weapon carried, and until weapons differ in more than damage a choice between
them has no decision in it.

**Tests:** each verb stage-1 parses; `enter the annex` moves toward it; `equip`
names the weapon combat will actually use.

---

## P6 — The session can be read back

**Files:** new `ui/transcript.py`, [\_\_main\_\_.py](../../src/simulacra/__main__.py),
[ui/tui.py](../../src/simulacra/ui/tui.py)

Nothing wrote a transcript, in either frontend. The fix is the event seam's
third renderer: **the REPL renderer, pointed at a file.** It already takes a
`Console`; a plain-text console on a file is a transcript, with no second copy
of the formatting to drift. Written to `saves/transcripts/<world>-run<N>.txt`
from both frontends through the observers they already fan out to.

**The TUI's quit moves off ctrl+c.** Textual's copy is ctrl+c, and the TUI bound
it to quit with priority, so selecting text and copying it closed the game.
Quit becomes ctrl+q; escape still works. Shift-drag keeps working as the
terminal's own selection.

**Tests:** a run leaves a transcript containing its prose and commands; no
spinner or control codes in it; ctrl+c is no longer bound to quit.

---

## P7 — Closeout from the post-plan report

- **The wary band gets a door.** Attacking an NPC was refused before it reached
  them, so `ATTACK_STEP` could never fire. It now costs disposition — no damage,
  NPCs are persistent — and says so.
- **Discoveries reach conversation.** They're canon on the `room:` node; the
  `room` route read only the concept.
- **Dead code:** `Recollection.age_runs` (named for an age, returned an id),
  `CallRecord.tok_per_s`, `dealings.NEUTRAL`.
- **plan.md §11** still calls floor persistence "not yet decided".
- **Every theme in `themes/` loads**, now that there is more than one.

---

## P8 — Measure: the playtest, replayed

Feed the playtest's own commands through a live game and check each question:

| Question | Resolved when |
|---|---|
| taken item still described | re-describing after `take` never names it |
| search adds nothing | bare `search` finds a fixture or says there's nothing; never re-describes the room |
| how did I get braced | `hide` says *hidden*, does something, prints once |
| equip | says what is in hand |
| logging / export | a transcript exists and reads like the session |

And count tier-1 calls across the replay against the original — this milestone
should have removed most of them.

---

## Order

P1, P3, P2 (search leans on P3's guard), P4, P5 (needs P4's `hide`), P6, P7, P8.

## Definition of done

- [x] A taken item never reappears in a description, including in old worlds
- [x] `search` costs no parser call and never re-describes the room as a find
- [x] No status is named for the opposite of what it does, and every status does something
- [x] One status line per turn
- [x] A verdict can't report a hit on something that isn't there
- [x] Every run leaves a readable transcript
- [x] Copying from the TUI no longer quits it *(the binding is gone and a test
      pins it; not yet verified by hand in a terminal)*
- [x] The wary band is reachable in play
- [x] Both themes load

---

## Findings

### P8 — the playtest, replayed

Same world — the archived playtest world, seed 213892568, copied so the original
was never touched — and the same commands, run once on pre-M11 `main` and once on
M11:

| Command | Before | After |
|---|---|---|
| **tier-1 calls, whole replay** | **13** | **1** |
| `look around` after `take water` | still describes the jar | doesn't — even in this old world, via the new cache key |
| `search` | re-describes the room (1 tier-1, 1 tier-2) | "You find nothing more here." (0 calls) |
| `search the threshold` | the same re-description again | the original find, replayed (0 calls) |
| `hide`, nothing hostile | the judge: *"Tipping a lit brazier into standing water to scald something is 12…"* | "There's nothing here to hide from." (0 calls) |
| `equip` | the judge: *"Equipping the jar of water allows the player to cast a healing spell…"* | "You have nothing to fight with but your hands." (0 calls) |
| `enter portal` | improvised, rolled, missed | the portal, found — it's in the room's concept (1 tier-2) |
| `search offcut`, offcut dead | re-discovered the room | "You don't see offcut here." (0 calls) |

The transcript that replay wrote is 60 lines with no escape codes, and reads like
the session.

### The one M11 didn't plan for: `persist_floor` erased discoveries every run

`search Lair of the Forgotten` on M11 should have replayed the playtest's find
there, and fell through to the room description instead. Comparing the room's
node across three copies of the world showed why:

| Copy | `found` index | Active finds |
|---|---|---|
| archive, untouched | `{lair: 8, forgotten: 8}` | 1 |
| after M11's replay | **gone** | 1 — still in the table, unreachable |
| after pre-M11's replay | `{lair: 19, forgotten: 19}` | **2** |

`persist_floor` runs in `begin()` on every run and wrote each room node's data
blob wholesale — erasing the `found` index M10 keeps in the same blob. So a
discovery could be replayed only in the run that made it. On the next run it
was unreachable, and the next search re-discovered it *as something new*: that
is the pre-M11 copy's second row, a different description of the same find,
silently spending budget. It's exactly the "a different altar every visit"
failure M10's D4 claimed to prevent.

**It has been live since M10 shipped**, and its test passed because it built
run 2's engine and never called `begin()` — the one step that persists the
floor. Fixed by merging into the existing blob; the test now calls `begin()`
and a second test calls `persist_floor` directly. Both were confirmed to fail
before the fix. Re-verified live on a fresh copy of the same world:
`search Lair of the Forgotten` replays canon #8, your original find, with the
index intact and still one row afterwards.

**The fourth of its shape.** M7's canonist had no output guard; M8's echo guard
had the wrong rule for dialogue; M9's node touch erased NPC state; M10's floor
persist erased room state. Each is a helper written when it had one caller,
whose behaviour became wrong when a second system started keeping state beside
it. Two of the four were `upsert_node` blobs being replaced rather than merged.
The durable rule: **a node's data blob is shared; nothing may write it
wholesale.**

### Legacy finds replay, by design

The archived playtest world holds ten discoveries that are really room
re-descriptions, made by the bug this milestone fixed. With the index preserved,
`search the threshold` in that world replays one of them rather than the room's
prose. That's deliberate: lookup comes before the room-itself rule, so what a
world showed you stays what it shows you. New worlds cannot make these. In old
worlds, each takes one slot of its room's budget of three.

### Two things the pre-M11 replay showed that the playtest didn't

**The judge has no echo guard.** Asked about `hide`, it answered with its own
calibration anchors — *"Tipping a lit brazier into standing water to scald
something is 12"* — the same prompt-transcription failure M2 built the
narrator's guard for. M11 took `hide` and `equip` away from the judge, so the
common cases are gone, but the judge itself is still unguarded.

**`jump` has no good home.** Before M11 the parser fallback routed it to
*movement*, walking the player into the next room. After, it routes it to a
whole-room look. Neither is right; it belongs with the judge. The fallback is
still the weakest link, and every verb it has to guess at is one more place
for it to guess wrong.

### Also noted

The Hardpan session that was running during this milestone started at 19:01;
M11's edits landed between 19:16 and 19:20. It was the pre-M11 build.

## Carried forward

- A guard on the judge's `reason`, in the manner of the narrator's.
- `jump` — probably straight to the judge as a stage-1 verb.
- Room-description variety — M12, with before/after word counts. The replay
  transcript's opening room still reaches for *"a shattered mirror … each one
  half-remembered and wrong."*
