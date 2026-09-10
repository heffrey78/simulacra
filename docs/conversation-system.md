# The conversation system

> **Superseded by M8.** This describes the single-channel system as it stood
> after T1: one embedding per question, one unconditional recite instruction,
> and no within-conversation state. All three are gone —
> [routes](tasks/M8-routes-tasks.md) replaced the channel, the instruction is
> now per-route, and a refusal is spoken without a model call at all. Kept
> because the analysis below is what produced that design, and because the
> transcript it dissects (eight questions, one fact, eight phrasings) is the
> "before" that M8 is measured against.

How `talk`/`talk to`/`speak`/`ask` — the four aliases that reach `_talk()` —
find an NPC, what they know, and why. Written up
while investigating conversation quality ahead of the POC closeout
([task doc](tasks/poc-closeout-tasks.md) covers unrelated parser/TUI bugs
found at the same time; this document is background for a design discussion,
not a task list).

Built in M4 ([task record](tasks/M4-tasks.md)). Tier 2 — same streaming/echo
discipline as room prose (plan.md §2).

---

## The pieces

| Piece | File | Job |
|---|---|---|
| `_talk()` | [engine/loop.py](../src/simulacra/engine/loop.py) | verb entry point, resolves which NPC, orchestrates the turn |
| `_recall_for()` | [engine/loop.py](../src/simulacra/engine/loop.py) | fetches what this NPC "remembers" |
| `Narrator.npc()` | [narrate/narrator.py](../src/simulacra/narrate/narrator.py) | the actual streamed LLM call |
| `Store.recall()` | [memory/store.py](../src/simulacra/memory/store.py) | hybrid graph + vector retrieval |
| `MemoryWriter` | [memory/writer.py](../src/simulacra/memory/writer.py) | writes the `Transcript` this turn produces, off the turn loop |
| `Npc` | [world/theme.py](../src/simulacra/world/theme.py) | static persona: `anchor`, `name`, `role`, `voice` |

## One turn, end to end

```
player types: ask Archivist about the arm
      │
      ▼
parser: verb=talk, target="archivist about the arm"   (stage 1 or 2)
      │
      ▼
Engine._talk(target)
      │
      ├─ resolve NPC: needle = target.lower(); first npc in room whose
      │  .name contains needle, else npcs[0]           ← see caveat below
      │
      ├─ persona = _personas()[actor.id]               (role, voice; the dict
      │                                                 is keyed by npc.anchor)
      │
      ├─ recollections = self._recall_for(actor.id)    (≤ 3, see below)
      │
      ├─ NpcPresent(remembers=bool(recollections))      renderer shows "(remembers you)"
      │
      ├─ stream: Narrator.npc(persona, target, recollections)
      │
      └─ Transcript(kind="dialogue", subjects=(actor.id, room), ...)
         → MemoryWriter records it, embeds it off-thread
```

**NPC resolution reuses `target`, unmodified, twice**: once as the substring
used to find *which* NPC is being addressed, and again as `player_line` — the
literal thing said to them. This works today because a room hosting an NPC
never hosts a second one (`test_nothing_hostile_shares_the_npc_room`,
enforced at generation), so the `next(..., npcs[0])` fallback always wins.
It's fragile if that constraint is ever relaxed, not urgent while it holds.

## What "remembers" actually means

`_recall_for()` embeds the player's **topic** — what's left of `target` after
`_topic_of()` strips the address (leading `to`/`with`, the NPC's own name,
then a leading `about`/`for`/`on`). `ask archivist about the arm` recalls
against `"arm"`. Only when nothing survives that strip — `talk`, `talk to
archivist` — does it fall back to the fixed string:

```python
DEATH_QUERY = "How did the previous delver die, and on which floor?"
```

Either way the embedding is anchored to the NPC via
`Store.recall(embedding=..., about=actor.id, exclude_run=state.run_id,
limit=3)`. `Store.recall` restricts candidates to memories whose
`memory_subjects` includes this NPC (graph-first), then ranks those by vector
distance, and `_recall_for` sorts deaths last (a small model attends hardest
to the end of its prompt).

`DEATH_QUERY`'s wording is load-bearing for the no-topic case and was arrived
at the hard way: M4's finding (M4-tasks.md, "The recall query was biased
toward chatter") was that a query phrased around *conversations* ranked
chatter above deaths, because those memories are literally about speaking. It
is phrased around *outcomes* on purpose. An NPC greeted with no question
volunteers the most important thing it knows; that is the intended behavior,
not a fallback that happens to work.

Until T1 ([task doc](tasks/conversation-improvements-tasks.md)) this query was
unconditional, so every question in a conversation retrieved the same top-N
recollections. That is fixed. What follows below is not.

## What the NPC is told to say

`Narrator.npc()` builds the prompt from persona + recollections, never from
conversation history:

```python
system = f"You are {npc.name}. {npc.role}. {npc.voice} Speak only as this
           character, in one or two sentences. ..."

# if recollections:
"You remember, from earlier delvers:
- <recollection 1>
- <recollection 2>
Say out loud what happened to the delver you remember, including the floor
and what killed them. Do not invent any other history."

# else:
"You remember nothing about this delver. Do not pretend to."

"The delver says: {player_line}"
```

Two things compound here:

1. **The instruction is unconditional.** Whenever there's at least one
   recollection, the model is told to recite it — not "answer the player's
   question using what you remember if relevant." M4's finding here was that
   a vaguer instruction ("refer to what you remember") produced a one-word
   non-answer, so this was made maximally explicit. It fixed that failure at
   the cost of making the NPC unable to talk about anything else.
2. **Every call is stateless.** No prior turn of the *same* conversation is
   in the prompt. The model doesn't know it already said "killed by a
   mechanical arm" three replies ago, so it says it again — from its
   perspective each question is the first one.

This is exactly what the sample transcript shows: eight different questions
("about the delver", "about the arm", "about their brother", "about the
incident") all converge on the one death recollection, phrased slightly
differently each time, because the recollections and the instruction are
identical on every call and nothing tracks what's already been said.

## What gets remembered afterward

`_talk()`'s `Transcript` records only the shape of the *question*, never the
answer:

```python
summary = f"A delver approached {actor.name} on floor {depth} and asked
            about {target or last_action}."
```

This is deliberate (M4: "the NPC was being fed its own words" — storing the
reply caused an NPC to quote itself back on later runs, crowding out real
death memories). The tradeoff: **nothing the NPC says, and nothing the player
tells the NPC, ever becomes new world knowledge.** The only rich memory any
NPC can ever draw on is a death `Transcript`, written by `_die()`, once per
run, worded by code (`_die()` in loop.py), not by the conversation.

So structurally, an NPC has exactly one class of thing to know (deaths) and
one behavior (recite the most relevant one), no matter how long the
conversation runs.

## Guards that do apply here

Dialogue reuses room prose's failure guards (`narrator.py`):
`looks_like_echo` (model transcribes the prompt instead of speaking) and
`looks_degenerate` (repetition collapse — this is literally how the M4 bug
report reads: the Archivist's first live reply repeated "1234567890" against
a motif list). Both apply to `npc()` exactly as they do to `room()`. Dialogue
is **never cached** (`prose_cache` is keyed for rooms; a verbatim replay
would be worse than a 6s wait) — every reply is freshly generated.

## Known limitations (as designed, not bugs)

- ~~Recall is topic-blind~~ — fixed by T1; recall now embeds the player's
  topic. But the *instruction* is still unconditional: an NPC handed a
  recollection recites it whether or not it answers the question (T2).
- No within-conversation memory: each ask is an independent call.
- No lore accumulates from conversation — only deaths are ever recorded with
  real content.
- Duplicate memories aren't deduplicated (noted in M4-tasks.md already).
- Only one NPC exists in the current theme pack, so multi-NPC-in-one-room
  resolution has never been exercised end to end.
