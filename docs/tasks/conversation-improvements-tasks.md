# Conversation system — recall, off-topic handling, in-conversation memory

> Scoped after reviewing [conversation-system.md](../conversation-system.md)
> against a live playthrough. Three fixes, deliberately not four —
> recording richer lore from conversation (letting an NPC learn things from
> what players *say*, not just from deaths) is out of scope here: it directly
> reopens the "NPC fed its own words" failure M4 already fixed once
> (M4-tasks.md, Finding 2) and deserves its own design pass, not a closeout
> item.

**Goal:** the Archivist should answer the question actually asked, not
recite the same death fact regardless of topic; should be able to say "I
don't know" instead of forcing an answer; and should not repeat itself
verbatim across consecutive replies in one conversation.

**Constraint that shapes all three:** tier 2 (plan.md §2) — ~180 output
tokens, streamed, prompt kept small. `qwen3:1.7b` has twice degenerated
(echoed the prompt; collapsed into repeating "1234567890") when given loose
or overloaded instructions. Every change here adds prompt content or prompt
branches — keep additions short, and keep instructions as unambiguous as the
existing "say what you remember, do not invent" line that already works.

---

## T1 — Query-aware recall

**Files:** [engine/loop.py](../../src/simulacra/engine/loop.py) (`_recall_for`)

**Current:** always embeds a fixed string — `"How did the previous delver
die, and on which floor?"` — regardless of what the player asked. Recall is
therefore topic-blind by construction (documented in conversation-system.md).

**Change:** embed the player's actual input instead, when there is one:

```python
def _recall_for(self, anchor: str, question: str = "") -> list[Recollection]:
    query = question.strip() or self._DEATH_QUERY   # fixed string, kept as fallback
    ...
```

**`target` cannot be passed verbatim as `question`.** The parser hands `_talk`
everything after the verb ([parser.py](../../src/simulacra/engine/parser.py),
`target = _strip_articles(rest)`), so `ask archivist about the arm` arrives as
the whole string `"archivist about the arm"` — the NPC's name and a
connective, embedded as though they were the topic. Two consequences:

1. The NPC name is noise present in *every* query alike, which shifts all
   distances by roughly the same amount and makes T2's threshold harder to
   site.
2. **The no-topic fallback would never fire for the command we actually
   ship.** `talk to archivist` parses to `target="archivist"` — non-empty —
   so `question.strip() or DEATH_QUERY` would embed `"archivist"` and the
   plain greeting silently stops volunteering the death.

So T1 is really two steps: extract a *topic* from `target` (strip the
resolved NPC's name and a leading `to`/`about`/`with`), then embed the topic
if anything survives, else the death query. Note `greet` and `tell` are not
verbs — the talk aliases are `talk to`, `talk`, `speak`, `ask` — so
"no-topic" in practice means bare `talk` or `talk to <npc>`.

**Keep the fixed-query fallback for the no-topic case** — an NPC volunteering
the single most important thing it knows on first contact, with nothing else
to go on, is good behavior and is what
`test_the_archivist_remembers_the_previous_run` already asserts. Only switch
to the player's own words when a topic survives extraction.

**`Store.recall()` needs no change** — it already accepts an arbitrary
embedding; this only changes what `_talk()` passes in.

**Regression test:** assert on the string handed to `client.embed`, not on
which memories come back. Three cases: `ask archivist about the arm` embeds
`"arm"` (the parser has already stripped articles by then — not the raw
target, not the death query); `talk to archivist` and bare `talk` both embed
the death query. The existing milestone test cannot catch a regression here on
its own — it drives `talk to archivist` through a fake client where every
embedding retrieves the same lone death, so it stays green whichever string is
embedded.

**Done** — [tests/test_conversation.py](../../tests/test_conversation.py).
`_topic_of()` in loop.py does the split in three head-only passes (address
words, NPC name, topic words); `_recall_for(anchor, question="")` embeds the
result or falls back to the now-module-level `DEATH_QUERY`. `_talk` still
passes the *raw* target to `Narrator.npc()` as `player_line` — the NPC hears
the whole line, it's only recall that wants the topic alone.

---

## T2 — Say "I don't know" instead of forcing an answer

**Files:** [engine/loop.py](../../src/simulacra/engine/loop.py) (`_talk`),
[narrate/narrator.py](../../src/simulacra/narrate/narrator.py) (`npc`)

**Depends on T1.** `Store.recall`'s hybrid path already returns a
`Recollection.distance` per result (semantic branch) — use it. When the
closest recollection's distance exceeds a threshold (needs calibration
against a live `nomic-embed-text` run, not guessed from unit tests — see
"Watch for"), treat the question as unanswerable from memory even though the
NPC has *some* recollections about other things.

**Three things about that distance signal, all easy to get wrong:**

- **Read it before the sort.** `_recall_for` returns
  `sorted(found, key=lambda r: r.kind == "death")` — deaths are deliberately
  moved to the *end*, so `results[0]` is not the closest match. Compute
  `min(r.distance for r in found)` on the pre-sort list.
- **Distance is a hardcoded `0.0` on the graph-only path.**
  ([store.py](../../src/simulacra/memory/store.py), `0.0 AS distance`.)
  `_recall_for` swallows an embed failure by setting `embedding=None`, which
  takes that path — so a threshold on distance alone reads an embedding
  outage as "maximally relevant" and recites regardless. Gate the branch on
  `via == "both"` as well, and treat the graph-only case as the current
  always-recite behavior.
- **The middle branch may be unreachable as recall stands.** The anchored
  path runs vec KNN with `k = limit` and intersects the result with the NPC's
  memory ids. Whether sqlite-vec pushes `v.memory_id IN (...)` down into the
  KNN as a rowid pre-filter, or SQLite post-filters the k nearest, decides
  whether an off-topic question returns one far-distance row or **zero** rows
  — and zero rows lands in the existing "you remember nothing about this
  delver" branch, not the new one. Check this empirically first: if it
  post-filters, T2 is mostly a pre-filter/`limit` fix in `Store.recall` and
  the prompt branch is the smaller half of the work.

**This needs a third prompt branch**, not just reuse of the existing
zero-recollection one. Today `Narrator.npc()` has exactly two states:
"you remember nothing about this delver" (cold start, no recollections
exist) and "here's what you remember, say it" (recollections exist, always
recite). Add the middle state — recollections exist, but not about *this*:

```python
# relevant recollections:
"Say out loud what happened to the delver you remember, including the floor
 and what killed them. Do not invent any other history."

# NEW: recollections exist but none are relevant to this question:
"You don't know anything about that. Say so briefly, in character. Do not
 mention the delver you do remember unless it's actually relevant."

# no recollections at all (cold start, unchanged):
"You remember nothing about this delver. Do not pretend to."
```

Wording matters more than usual here — this is new prompt surface for a
model that has twice degenerated on loose instructions. Keep it as terse and
closed as the existing branches.

**Regression test:** a question whose recall distance is above threshold
produces the "don't know" branch (assert on prompt contents sent to
`FakeClient`, per M4's own rule — "assert on the prompt, not the output").

---

## T3 — Short-lived in-conversation memory

**Files:** [engine/loop.py](../../src/simulacra/engine/loop.py) (`Engine`,
`_talk`), [narrate/narrator.py](../../src/simulacra/narrate/narrator.py) (`npc`)

**Problem:** every `npc()` call is stateless — the model doesn't know it
already answered this question two replies ago in the same conversation, so
consecutive questions in one visit produce near-identical restatements (the
live transcript: eight questions, one fact, eight phrasings).

**Change:** track the last 1–2 exchanges **per NPC, per run, in engine
state** — not in `Store`. This is the load-bearing distinction versus T-none:
M4's "fed its own words" bug happened because a reply was written to
*persistent, cross-run* memory. A same-run, in-memory-only scratch buffer
that dies with the process cannot cause that bug — it never reaches SQLite,
never gets embedded, and is gone next run.

```python
# Engine.__init__
self._conversation: dict[str, list[tuple[str, str]]] = {}  # npc.id -> [(asked, said)]
```

- Append `(target, spoken_text)` after each `_talk()` call; cap at the last 2
  pairs (drop oldest). **Skip empty replies** — when the echo or degeneracy
  guard fires, `Narrator.npc()` clears its buffer and yields nothing, so
  `spoken` is `[]`. Storing that puts a bare `You said:` with nothing after it
  into the next prompt, which is exactly the kind of loose surface this model
  has degenerated on before.
- Pass into `Narrator.npc()` as a distinct, clearly-labeled prompt section —
  **do not merge it with the cross-run `recollections` list**, the model
  needs to be able to tell "something I said five seconds ago" apart from
  "something a graph anchor told me about a prior delver." E.g.:
  ```
  Earlier in this conversation:
  Delver asked: {q}
  You said: {a}
  ```
- Clear the whole dict on `RunEnded` (new run, nothing carries over) — it
  already doesn't need clearing on room exit; remembering the last thing you
  told the Archivist if the player wanders off and comes back in the same run
  is reasonable, and simpler than adding a room-exit hook.

**Regression test:** asking the same NPC two related questions in one run
produces a prompt on the second call that contains the first Q/A pair. The
clearing half can't be written as "a fresh run's first prompt has no
conversation section" — an `Engine` is built once per `new_run` and both
frontends break out of their loop on `RunEnded`
([\_\_main\_\_.py](../../src/simulacra/__main__.py)), so a run *is* a process
and the dict is already gone. Assert directly that the dict is empty after
the engine emits `RunEnded`; the clear is insurance for a future in-process
restart, not observable behavior today.

---

## Order and dependencies

T1 → T2 (T2 needs T1's distance signal) → T3 is independent of both and can
land in parallel with either. Suggested order: T1, then T3 (independently
testable, no threshold-tuning risk), then T2 last since it's the one that
needs live-model calibration.

## Definition of done

- [x] Different questions to the same NPC retrieve different recollections
      when the player's own words are embedded, not the fixed death query
- [x] The embedded string is the *topic*, with the NPC's name and the
      leading connective stripped — `ask archivist about the arm` embeds
      `"arm"`
- [x] `talk to archivist` (not just bare `talk`) still gets the current
      volunteer-the-death behavior — the NPC name alone is not a topic
- [ ] A genuinely off-topic question gets an in-character "I don't know",
      not a forced recitation of an unrelated memory
- [ ] Two related questions in the same conversation don't produce
      byte-for-byte repeated phrasing (verified live, not just by prompt
      contents — see Watch for)
- [ ] Conversation scratch state never touches `Store`; a new run starts
      with none

## Watch for

**Threshold calibration is not a unit-test problem.** `FakeClient` returns
deterministic/zero vectors, so the "relevant vs off-topic" distance cutoff in
T2 can only be exercised with fake, chosen-for-the-test distances — it has to
be tuned against a live `nomic-embed-text` run the same way M4's echo guard
and degeneracy guard were, by playing it and reading the actual distances
that come back.

**The demo passing for the wrong reason** (M4's own warning, still
applicable): a plausible-sounding "I don't know" is easy to get from a small
model even when the underlying distance logic is broken. Assert on the
computed distance and the prompt branch taken, not just that the output
*sounds* like a refusal.

**Prompt growth.** T2 adds a branch, T3 adds a section — both push the
tier-2 prompt (~600 token budget, plan.md §1) upward. Small individually;
worth a token-count sanity check once both are in, not just each in
isolation.
