# M15 — Search adds to the room, and run 3's small fixes

> Roadmap [R1 and R2](../roadmap.md). From the
> [third playtest's review](../playtests/2026-09-11b.md): the one thing the
> player flagged was that `search` re-describes the room instead of adding to
> it, and the transcript showed five smaller things.

**Goal:** a search turns up one thing the room hadn't said, names it, and
leaves it in the room. None of run 3's small failures happens again.

**Constraint:** a search still costs at most one model call when it works, and
at most two when the first is refused. Everything in R2 is code.

---

## R1 — search

What the run's 29 finds did, and why:

| | count | cause |
|---|---|---|
| used the fixture's own word | 10 of 29 | nothing required it |
| re-described what the room already said | 2 | the prompt never had the room's description |
| put the fixture on the delver's body | 6 | the room narrator's brief: second person, "you stand…" |
| follow-ups on a find's nouns that worked | 0 of 5 | only the fixture's words reach a find |
| finds shown by `look` afterwards | none | the room's prose is cached and never grows |

The fixes:

| # | Fix | Files |
|---|---|---|
| S1 | The search prompt carries the room's description, as `DESCRIPTION:`, and asks for something it doesn't already say | `narrator.py` |
| S2 | The find must open with its fixture's name. `names_its_find` checks the head before anything is shown. One strict retry, then "Nothing comes of the search." and nothing is written down | `narrator.py` |
| S3 | The description is in the repeat check's census, and `description:` is a label the output may not contain | `narrator.py` |
| S4 | Each find's name is kept on the room (`found_names`), and `look` ends with "Noticed here: …". Finds from before M15 are named from the theme's fixture pool | `loop.py` |
| S5 | `take` a noun a find described: "That's part of the room, not something you can carry." | `loop.py` |

**Changed from the roadmap's plan:**
- **The engine doesn't print "You look closer: a harness." before the find.**
  S2 makes the model's first words the name, so the line would say it twice.
- **A find's other nouns don't lead back to it when you `look at` them, only
  when you `take` them.** M10 found this live: `look at the surface` replayed
  the door, because the door's find used the word "surface".
  `test_a_repeat_look_is_keyed_on_what_was_searched` holds that rule, and M15
  keeps it. A refusal can't go wrong in that way, so `take` gets the words.

## R2 — the small fixes

| # | Transcript | Fix | Files |
|---|---|---|---|
| F1 | 18 of floor 6's 73 commands: "There are no stairs here." | `RoomEntered.unexplored`; the exits line ends "(unexplored: S W)" in both renderers | `events.py`, `loop.py`, `repl.py`, `tui.py` |
| F2 | "greet Powder Monkey" → "You are not carrying that." | `greet`, `hail`, `hello`, `hi` are `talk` in stage 1 | `parser.py` |
| F3 | `talk to Powder Monkey`: addressee "powder", topic "monkey" | Once the NPC is resolved, the rest of its name comes off the topic, for `talk` and `tell` | `loop.py` |
| F4 | Eleven inventory lines, four the same tin of peaches | Grouped by name and value: "a tin of peaches ×2 (heals 4)" | `loop.py` |
| F5 | "…after thirty-eight turns because he refuses to move as" | The epitaph prompt has no numbers and says "they". `one_sentence` keeps the first whole sentence, or nothing if it was cut off or counts anything | `narrator.py` |

**F3 is the engine's job.** The parser can't know names, so it takes one word
as the addressee when there's no connective, and says so in its docstring.
Only the engine knows who is in the room.

**F5 drops rather than repairs.** A count in an epitaph can only contradict
the engine's own line, "Floor 7, 308 turns.", and a sentence cut off can't be
finished in code. No epitaph is better than either.

---

## Definition of done

- [x] A search's prompt carries the description the player read
- [x] A find that doesn't name its fixture is refused, retried once, and never written down
- [x] `look` lists a room's finds, in this run and the next, including finds from before M15
- [x] `take` a noun a find described is "part of the room"
- [x] `greet` is stage 1; a two-word NPC name is not a topic
- [x] `inventory` groups; the exits line marks unexplored exits
- [x] The epitaph is one whole sentence with no count, or nothing
- [x] Every new test failed on the committed build first
- [x] Measured live against the committed build

---

## Findings

### Search, measured live

I rebuilt the run's 29 search rooms from a copy of its world, with their
descriptions, and asked for each fixture twice on `qwen3.5:2b`: 58 searches on
the committed build (in a worktree), and 58 on M15 as it ships.

| | committed | M15 |
|---|---|---|
| finds shown that name their fixture | 18 of 58 | **49 of 49** |
| open with "You" | 30 | **0** |
| on the delver's body (loose count) | 24 | 9 |
| refused: "Nothing comes of the search." | 0 | 9 |

The finds are about the thing now:
- *"Harness hangs suspended from a rusted chain…"* was the room's own lever.
- *"The tag board hangs from a rusted wire…"* was *"an iron sheet"*.
- *"A strapped chest sits rigid on a bed of rusted scales…"*

**The refusals are the cost.** Before M15, every search printed something,
and 40 of 58 didn't name what was found. Now 9 of 58 say *"Nothing comes of
the search."*, after two model calls. That's down from 15 on the first
measured build; the next section is why. The loose count of the delver's body
still catches some: *"A boot scraper rests against your ankle"*. Nothing
checks for that. The prompt asks for the thing and not the player, and the 2b
mostly listens.

### Why a find was refused, and what that changed

On the first measured build, 15 of 58 searches were refused. To find out why,
I asked the 14 refused room-and-fixture pairs again with the same first
prompt, three times each. This time I kept the whole reply and recorded where
it first named its fixture:

| 42 fresh tries at the refused pairs | count |
|---|---|
| named it in the first 48 characters, so the check would pass | 24 |
| named it at 49–96 | 1 |
| named it only after 96, having described something else first | 5 |
| never named it | 12 |

- **A wider window buys nothing.** One reply in 42 named its fixture just past
  the check. The five that named it late had opened on something else: *"The
  metal plate beneath your boots glows…"* for the assay scales. That's the
  re-description the check exists to catch.
- **Most refusals are bad luck twice.** The same pairs pass 24 times in 42. A
  refused search writes nothing down, so searching again asks for the same
  fixture, and has about even odds.
- **Three of the twelve "never" were the right word in another form:** *"the
  timbers"* for timbering (twice) and *"the straps"* for a strapped chest. A
  shared five-letter stem now counts as naming it.
- **The rest were rightly refused.** *"The vertebrae"* and *"the skeleton"* for
  bones, *"a heavy iron handle"* for a boot scraper, *"It stands like a jagged
  finger of white wax"* for a candle stub. None can be followed up by the
  fixture's name, so none can join the room.

### F5, measured live: the counts are gone, and "he" isn't

Ten epitaphs each for the run's own death (the Company Man, floor 7, turn 308),
on `qwen3.5:2b`, from the committed build and from M15:

| | committed | M15 |
|---|---|---|
| counts something | 10 of 10 | **0** |
| cut off mid-sentence | 5 of 10 | **0** |
| dropped, so no epitaph | 0 | 1 |
| opens with "You" | 6 of 10 | 1 of 10 |

On the committed build, every epitaph counted something the engine never said:
*"three days ago"*, *"three hundred and eight rounds"*, *"number seven"*. Half
ran out of tokens mid-sentence. M15's are whole sentences with no count.

**Asking for "they" didn't work.** At least 3 of M15's 10 call the delver
"he": *"The delver fell to his knees…"*. Some also invent people: *"a young
man named Thomas"*, *"a young girl with silver teeth"*. There's no safe way to
check for this in code, because "he" is right for the Company Man. It's the
2b's invention problem again, and it's recorded under
[R3](../roadmap.md#r3--npc-dialogue-invents-numbers-and-plays-its-role-literally).

### The tests failed first

On the committed build, all 32 of `test_playtest_d.py`'s tests fail:
- 18 because the committed build does the wrong thing;
- 14 because the helper they test is new: `names_its_find` (9),
  `one_sentence` (4) and `_past_the_name` (1). Two checks among these match
  what the committed build already did: "No one comes down for the delver." is
  kept whole (the word "one" isn't a count), and "widow" isn't taken as the
  rest of a name.

The suite is 930, up from 894: the 32 new tests, and 4 cases an existing test
generates, one for each new greeting alias.

Two M11 tests changed. Their stand-in model wrote a find that named no
fixture, which M15 refuses. The stand-in now names what it was asked about.
