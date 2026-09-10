"""Derived canon: the tier-3 call that grows an NPC's backstory one run at a time.

**The rule this module exists to enforce**, from systems.md §5:

    derived canon is generated from `authored` and `observed` only -- never from
    other `derived` canon, and never from `generated` prose.

It does two jobs at once. It closes M4's self-quotation loop, where an NPC's own
reply was stored as fact, recalled, and fed back into the prompt that produced
it. And it caps generation depth at exactly **1**, so canon cannot drift by
compounding on its own output across a world's lifetime -- model output is a
leaf, always.

That rule lives in `_inputs()`, in the query, not in a comment. There is no code
path here that reads a `derived` row.

**When this runs.** At exit, not at startup. `Session.close()` already runs
while the player is reading an epitaph, which is dead time this can spend for
free; and refreshing *after* a run is what makes canon grow by one increment per
run, which is what "progressively created" means.

MILESTONE M7.
"""

from __future__ import annotations

import json
import re
import threading

from ..narrate.narrator import looks_degenerate

CANON_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
    },
    "required": ["facts"],
}

# How much derived canon one NPC keeps. Canon that only ever grows becomes a
# prompt-bloat problem some number of runs out, and a cap is far cheaper than
# the compaction pass it would otherwise need.
DERIVED_CAP = 4

# Episodic memories handed to one refresh. Enough to say something from, few
# enough to stay inside the tier-3 budget.
MEMORY_WINDOW = 6

# Seconds the exit will wait for this before walking away from it. A player
# closing the game must close the game -- an abandoned refresh costs one run's
# increment, which is nothing next to a 120 s hang on quit.
REFRESH_TIMEOUT = 35.0


# Kinds worth writing someone up from, best first. A `dialogue` transcript
# records the shape of a question and not its answer -- "a delver asked about
# the arm" -- which is true and nearly information-free. Handed a prompt full of
# those, the model says the same thing about the same noun every run.
INFORMATIVE = ("death", "discovery", "event", "dialogue")

# A raw digit run in a sentence about a dungeon NPC is the signature of the
# collapse M4 caught in the narrator ("1234567890" against every motif). Four
# digits, so "floor 3" and "209 delvers" survive.
_DIGIT_RUN = re.compile(r"\d{4,}")

# The authored canon lines are eight to twelve words, and up to four of these
# go into every conversation this NPC ever has. Past about two dozen words it is
# not a fact any more, it is a paragraph.
MIN_WORDS, MAX_WORDS = 4, 24


def _plausible(text: str) -> bool:
    """Reject model output before it becomes permanent.

    The narrator has had `looks_degenerate` since M4, but it guards *streamed
    prose* -- the canonist writes structured output straight into a table that
    outlives the run, with no guard at all. Measured on the first live five-run
    read: "The Archivist's ledger is currently marked with 123456789."

    Canon is forever in a way prose is not. A bad line of narration scrolls
    away; a bad line of canon is in every conversation that NPC ever has again.
    """
    words = text.split()
    if not (MIN_WORDS <= len(words) <= MAX_WORDS):
        return False
    if _DIGIT_RUN.search(text):
        return False
    return not looks_degenerate(text)


def _inputs(store, anchor: str) -> tuple[list[str], list[str]]:
    """Authored canon and episodic memory. **Never derived canon.**

    The depth-1 rule is this function. `provenance="authored"` is the whole
    guard, and `test_the_refresh_never_reads_derived_canon` is what keeps a
    later edit from quietly widening it.
    """
    authored = [r["text"] for r in store.canon(anchor, provenance="authored")]
    memories = [
        r["text"] for r in store.memories_about(
            anchor, prefer=INFORMATIVE, limit=MEMORY_WINDOW
        )
    ]
    return authored, memories


def _messages(npc, authored: list[str], memories: list[str]) -> list[dict]:
    known = "\n".join(f"- {a}" for a in authored) or "- nothing recorded"
    seen = "\n".join(f"- {m}" for m in memories) or "- nothing yet"
    return [
        {"role": "system", "content":
            f"You keep a factual dossier on {npc.name}, who {npc.role}. "
            f"Write plain statements of fact. Be terse. Do not write dialogue."},
        {"role": "user", "content":
            f"Known about {npc.name}:\n{known}\n\n"
            f"What has happened near them:\n{seen}\n\n"
            f"Write up to three NEW short factual sentences about {npc.name} "
            f"that follow from what is above. Do not repeat a known fact. "
            f"Do not invent events that are not listed."},
    ]


def _candidate(store, theme):
    """The NPC with the most episodic memory it has not been written up from.

    One per exit, so the cost of quitting is bounded by construction rather than
    by however many NPCs a theme pack happens to define.

    Skipping an NPC whose memory has not grown is not an optimisation -- without
    it, every exit burns a tier-3 call regenerating identical canon.
    """
    best = None
    for npc in theme.npcs:
        node = store.node(npc.anchor)
        if node is None:
            continue
        data = json.loads(node["data"] or "{}")
        seen = int(data.get("canon_memories") or 0)
        have = store.memory_count(npc.anchor)
        if have > seen and (best is None or have - seen > best[1]):
            best = (npc, have - seen, have, data, node)
    return best


def _refresh_one(store, theme, client, policy, run_id: int) -> list[str]:
    found = _candidate(store, theme)
    if found is None:
        return []
    npc, _grown, have, data, node = found

    authored, memories = _inputs(store, npc.anchor)
    reply = client.structured(_messages(npc, authored, memories), CANON_SCHEMA,
                              policy, kind="tier3")

    written: list[str] = []
    known = {c.lower() for c in authored}
    known |= {r["text"].lower() for r in store.canon(npc.anchor, provenance="derived")}
    for fact in (reply or {}).get("facts", [])[:3]:
        text = str(fact).strip()
        if not text or text.lower() in known or not _plausible(text):
            continue
        store.add_canon(npc.anchor, text, "derived", source_run=run_id)
        written.append(text)

    store.retire_canon(npc.anchor, provenance="derived", keep_newest=DERIVED_CAP)

    data["canon_memories"] = have
    data["last_canon_run"] = run_id
    store.upsert_node(npc.anchor, "npc", npc.name, data)
    store.commit()
    return written


def refresh(store, theme, client, policy, *, run_id: int,
            timeout: float = REFRESH_TIMEOUT) -> list[str]:
    """Grow one NPC's canon. Never raises, never hangs the caller.

    Runs on a daemon thread so a model that stops responding costs a bounded
    wait rather than the client's full request timeout. Abandoning it loses one
    run's increment, which the next exit will pick up -- the gate is a memory
    count, not a flag, so nothing is lost by not finishing.
    """
    if client is None:
        return []

    out: list[str] = []

    def work():
        try:
            out.extend(_refresh_one(store, theme, client, policy, run_id))
        except Exception:
            # Same contract as the epitaph: a failed flourish is not an error.
            pass

    t = threading.Thread(target=work, name="canonist", daemon=True)
    t.start()
    t.join(timeout)
    return list(out)
