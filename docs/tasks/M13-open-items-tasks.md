# M13 — Open items · ✅ COMPLETE

> **Status: done.** 833 tests green with the daemon unreachable. Measured live
> against the committed build, which caught a regression of M13's own. The
> judge's defensive kind was probed live before it shipped. Findings at the
> bottom.

> Every item recorded as open, from any milestone or playtest, in one place,
> with what happened to each. Sources: M11's "Carried forward", M11.1's "Seen in
> the live check", M12's "What M12 does not fix", M5's recorded gap, the
> deferred rows of M7 and M8, and both [2026-09-10 playtests](../playtests/).

**Goal:** after this there is exactly one list of what is still open, and every
item on it has a reason and a home. Anything fixable without a design decision is
closed here with a test that failed first.

**Constraint:** no new model calls. The judge and dialogue fixes change what goes
*into* calls we already make, or what code does with what comes out.

---

## The ledger

| # | Item | Recorded in | Now |
|---|---|---|---|
| O1 | The judge has no echo guard | M11 findings | **fixed** |
| O2 | The judge's reason promises what the vocabulary can't do | M11.1 | **fixed** |
| O3 | The judge can't see the pack | M11.1 | **fixed** |
| O4 | A recalled past run is narrated, not spoken | M11.1 | **fixed** |
| O5 | Hardpan's refused floor names fall back to "Floor N" | M12 | **fixed** |
| O6 | Floor 1's name and goal never reach the TUI header | M5 (known gap) | **fixed** |
| O7 | `uv run` uninstalls pytest and Textual | M11.1 | **fixed** (`3b797b2`) |
| O8 | Canon can hold contradictory facts | M7, M8 deferred, playtest 1 | **decided: hold both** — nothing to build |
| O9 | `jump` has no good home | M11 | closed by M11.1 — an improvisation, with a roll |
| O10 | TUI text can't be selected or copied | playtest 1 | closed by M11 — every run writes a transcript |
| O11 | Hearsay repeated without attribution | M7 | closed by M8 — attribution is in the data |
| O12 | The model's own tics: *something, thick, scent* | M12 | **open** — see below |
| O13 | Looting, corpses, drops | M10, M11 | **designed** — [M14](M14-looting-tasks.md) |
| O14 | Weapons with tradeoffs | M11 | **open** — M14's depth-scaled values set it up |
| O15 | Occasional factual drift in derived canon | M7 | **accepted** — bounded by `DERIVED_CAP` |
| O16 | Summarising old episodic memory | M7 deferred | **open** — no pressure yet |
| O17 | NPCs asking questions back; multi-turn plans | M8 deferred | **open** |
| O18 | Floors 1–3 have no vault (slot collision), so no weapon | M14 findings | **fixed** — [M14.1](M14.1-fixes-tasks.md): the weapon, from its own stream, without re-roling rooms |

---

## O1 — The judge's reason is guarded like the narrator's prose

**Files:** [engine/judge.py](../../src/simulacra/engine/judge.py)

M11's pre-fix replay asked the judge about `hide` and got its own calibration
anchors back: *"Tipping a lit brazier into standing water to scald something is
12."* That is the M2 prompt-transcription failure arriving at the judge. A
reason containing a phrase that exists only in the judge's prompt
(*calibrate, difficulty, brazier, standing water, Room:, Carrying:*…), and not
in what the player typed or the room, is refused. It is refused rather than
retried, because a model that recited its instructions has not understood the
action. Its other fields are no better than its reason, and a failed call is
never a free win.

## O2 — What the reason promises, the engine can do, and says it did

**Files:** `judge.py`, [engine/combat.py](../../src/simulacra/engine/combat.py)

`dodge` was ruled *"prevents enemy from attacking, reduces damage taken by 3"*,
and the dry hand attacked on the same turn. The reason is free text and the
effect a closed enum, and nothing made them agree. There are three parts to
the fix:

- **A defensive effect exists.** The parser has sent `dodge`, `duck` and `roll`
  to the judge since M11.1, and the vocabulary had nothing to give them. The
  kind `defend` becomes `guard_self`, which puts the player on guard (+3
  defense) for the monster round it was made against. It is a new effect, not a
  new subsystem: combat has read statuses since M11. It became the player's
  guard whoever the ruling names as affected, and each kind is glossed in the
  prompt. Both choices came from a live probe; see Findings.
- **The reason carries no numbers.** The engine clamps amounts after the model
  answers, so a number the model states may not be the one applied. Clauses
  stating a number are dropped whenever one without survives.
- **Every applied effect is stated in the engine's own words.**
  `status_target` lowered the enemy's defense *silently*; it now says so. So
  does a guard, and so does a success whose effect is `nothing`. The reason is
  the model's account; the line after the roll is the truth.

## O3 — The judge sees what the player carries

**Files:** `judge.py`, [engine/loop.py](../../src/simulacra/engine/loop.py)

`water` was ruled *"pouring water onto the floor to cool off"* with no water
carried. The judge's prompt now has a `Carrying:` line, and a rule that an
action needing something neither carried nor visible is not plausible. The pack
goes to the judge only, not into `_room_summary`: that string is also the parser
fallback's context, and anything in it is a target the fallback can lift. That
is the shared-helper lesson from M11.1, applied before it could bite.

## O4 — A recalled run is spoken by the one who remembers it

**Files:** [engine/routes.py](../../src/simulacra/engine/routes.py), `loop.py`

*"The delver approached the Assayer on floor 1 and asked about assayer. They
were killed by a falling weight."* The recollection was right; the voice was a
narrator's. There were three causes, and each gets a fix:

- **The memory was about the NPC, handed to the NPC.** Memories are written
  from outside ("approached the Assayer"). The `past` route now addresses each
  one to its listener ("approached *you*") before the model sees it. A 1.7b
  reads data back far more faithfully than it follows an instruction, so the
  data itself has to be speakable.
- **The instruction said "say out loud what happened"**, which it obeyed as a
  report. It now says who is being spoken to, and in which person.
- **"asked about assayer" was never asked.** A topicless greeting fell back to
  recording the *addressee* as the topic. That became a memory, and a later
  run recalled the question nobody had asked. The topic alone is recorded now.

## O5 — Hardpan has a floor title of its own

**Files:** [themes/hardpan.toml](../../themes/hardpan.toml)

`floor_name = "The {nth} Level"`. A mine has levels. Offline descents now use
the theme's title too, where they used to say "floor 2".

## O6 — Floor 1 is named to the renderers

**Files:** [engine/events.py](../../src/simulacra/engine/events.py), `loop.py`,
[ui/tui.py](../../src/simulacra/ui/tui.py)

M5 recorded the gap and the constraint. Only a descent carried a floor's
identity. Emitting `FloorDescended` from `begin()` would print "You descend to
floor 1" in the REPL, so M5 asked for a new event, "decided deliberately". Here
it is: `FloorNamed`. `begin()` emits it once, after the floor is established.
The TUI's header reads it, and the REPL ignores it.

## O8 — Canon holds both

The first playtest decided it: when new canon contradicts old, keep both *"to
add to the mystery"*. That is what the store already does. Nothing supersedes
anything, and a contradiction is two active rows. So M7's "superseded facts
are not detected" stops being a limitation, and M8's deferred row
"contradiction detection" is closed as **won't build**.

The one thing that retires derived canon is `DERIVED_CAP`, which keeps the
newest four per subject. That is a prompt-size budget, not a verdict on truth,
and it stays.

## Still open, and why

- **O12 — the model's tics.** After M12, the words spread across half a floor's
  rooms are the model's own: *something, thick, scent, forgotten*. They come
  from no prompt. The available lever is the theme's `banned` list, and banning
  common words on a 1.7b tends to trade one tic for another. It's worth a
  measured pass of its own, with the M12 metric, and it is theme work rather
  than engine work.
- **O13 — looting.** Every drop moves M3's balance curve. M10 kept discoveries
  as scenery for exactly this reason. It needs its own milestone with a balance
  test, not a rider on another.
- **O14 — weapon tradeoffs.** Until weapons differ in more than damage,
  choosing between them isn't a decision. `equip` tells you what's in hand
  (M11). This waits on O13, which is where new weapons would come from.
- **O15 — factual drift.** *"The Archivist remains on floor 3"* is rare,
  bounded by the cap, and cheaper to live with than to prompt against.
- **O16 — memory compaction.** No world has grown enough episodic memory to
  slow recall. It's worth revisiting when one does.
- **O17 — NPCs that ask back.** A conversation feature, not a fix. It needs its
  own design.

## Definition of done

- [x] A judge reason reciting its prompt is refused; the player's own words are not
- [x] `dodge` can be ruled a guard, and a guard changes the monster round
- [x] Every applied effect is stated by the engine
- [x] The judge's prompt carries the pack; the parser fallback's does not
- [x] A recalled memory reaches the NPC addressed to it
- [x] A topicless greeting is not recorded as a question
- [x] Floor 1's name and goal reach the TUI header; the REPL prints nothing new
- [x] A heal needs something to heal with — found by the measurement, below
- [x] Every new test failed on the committed build first — 21 of the 24 new or
      changed tests; the other three are no-regression guards (the player's own words are not an echo; a
      reason with nothing but numbers is kept; a guard expires after its round)
- [x] Measured live against the committed build

---

## Findings

### Measured live

This ran on Hardpan with `qwen3:1.7b`, using the committed build (`fa5eee6`, in a
worktree) and then M13, through the same script. The judge got five actions,
five samples each, with the model's raw responses recorded beside the verdicts
the engine let through. The past-run test was the Assayer holding a previous
run's death and the M11.1 dialogue memory, asked six times.

| | committed | M13 |
|---|---|---|
| past-run replies narrated | 6 of 6 | **0 of 6** |
| past-run replies in the first person | 0 of 6 | **6 of 6** |
| judge reasons stating a number | 12 of 25 | **0 of 25** |
| judge reasons reciting the prompt | 0 of 25 | 0 of 25 |
| heals ruled with nothing to heal with | 0 of 10 | 10 of 10, then **0** — see below |
| `dodge` ruled a guard | — (no such effect) | 0 of 5 at first; **3 of 3** as shipped † |

Before, the Assayer read the memory back six times, word for word: *"A delver
was killed by a falling weight on floor 1."* After: *"I have not been paid
since the mail stopped coming up the grade. I buy anything by weight and nothing
by story. The delver before me was killed by a falling weight on floor 1."* Its
canon comes first, then the memory, in its own voice. The M8 rule dropped the
dialogue memory in both builds, so what reached the model never named the
Assayer. The instruction did this on its own; the rewrite to "you" rests on its
unit tests.

**The echo did not reproduce**, 0 of 25 on either build. That guard rests on
its tests too.

### The measurement found a regression, and the fix is code

The first M13 run ruled "pour water over my head", with nothing carried, a heal
five times in five: *"heals player due to natural moisture"*. `hide` in an empty
room became a heal as well, and its reason said *"providing temporary
protection"*. The committed build had ruled both `status_self`: a penalty
under a helpful-sounding reason, which is the O2 mismatch again. The new
`Carrying: nothing` line and the rule beside it were ignored. And offered a
`protect` kind, the model filed protection under `heal`.

A heal is the one effect of pure gain with no enemy involved. So, like a verdict
aimed at an enemy, it now needs something to act through: an item carried or in
the room with `heal > 0`. Without one there is no roll, and the player reads
*"You have nothing here to heal with."* That is M11's phantom-enemy rule applied
to the other free win. `hide` never reaches the judge in play, because M11 made
it a verb, but the confusion behind that ruling can.

**Longer reasons.** After the rewording, reasons ran to two sentences where one
is asked for, and `jump` in an empty room invented a table and an ambush. Code
still decides what those rulings do, so this is recorded rather than fixed.

**One edge in the "you" rewrite.** It replaces the NPC's bare name anywhere in
a memory, so a memory mentioning *"the Widow's charge"* would become *"you's
charge"*. No memory the engine writes today names an NPC except as the one
approached, or as a topic.

### † `protect` was never chosen, so the vocabulary was probed before shipping

The first M13 run offered `protect`, and the model chose it 0 times in 25. Its
reasons said *evasion* while its kind said `hinder`, and it put *protection*
under `heal`. Rather than guess a replacement, I probed four wordings live. There
were six actions, three samples each, and the shipped prompt was otherwise
unchanged. The model gave the same answer on every sample of a cell, except
where marked.

| action (a right ruling) | `protect` | `defend` | glossed | **glossed + verbs** |
|---|---|---|---|---|
| dodge (defend) | enemy/hinder | enemy/damage | enemy/hinder | **enemy/defend** |
| duck behind the ore cart (defend) | enemy/hinder | enemy/hinder | enemy/hinder | enemy/hinder |
| raise the pick handle to block (defend) | enemy/hinder | enemy/hinder | enemy/defend | **enemy/defend** |
| throw the pick handle (damage) | enemy/damage | enemy/damage | enemy/damage | enemy/damage |
| trip the dry hand (hinder) | damage ×2, hinder ×1 | enemy/damage | enemy/hinder | enemy/hinder |
| pour water over my head, nothing carried (not a heal) | you/heal | you/heal | you/hinder | you/hinder |
| **right, of six** | 1 | 1 | 4 | **5** |

Two findings shaped the judge that shipped:

- **`affects` was `enemy` every time the model chose `defend`**, because the
  enemy is who a defence is against. Mapped only from `(you, defend)`, the
  winning wording would have produced no guards at all. So `defend` becomes
  the player's guard whoever the ruling names.
- **Glossing every kind helped the kinds that weren't the target.** Trip became
  `hinder`, and the free heal disappeared at the prompt as well as in code.

What shipped is the glossed prompt, with `defend` described by the verbs the
parser sends: dodge, duck, block, brace. *"Duck behind the ore cart"* still goes
to `hinder`; to the model an ore cart is cover used against the enemy. When it
does, the engine now says what the ruling actually did: *"A dry hand is left
open."*
