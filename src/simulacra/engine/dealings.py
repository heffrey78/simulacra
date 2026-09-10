"""Trade, gifts, and what an NPC thinks of you.

The second instance of the pattern [judge.py](judge.py) validates -- a closed
enum, code checking the decision against state the model does not get to assert,
and code clamping the result -- not a second copy of it. The judge resolves a
*physical* action with dice (`plausible`, `difficulty`, `magnitude`); an NPC
deciding whether to take a gift is a *social* choice with no dice in it, and
forcing one schema over both would make each worse.

Two things are deliberately **not** in the enum:

* **`attack`.** An NPC that can decide to attack is a monster, and floorgen
  already makes those. Monster behaviour stays in `combat.py`, seedable and
  testable without a model -- routing it through here would put a tier-1 call on
  every combat turn.
* **`follow` / `lead` / `move`.** The player asks and *disposition* answers,
  which is code's call. Asking a 1.7b whether it feels like walking with you is
  spending a model call on a decision code can make correctly every time.

**Every refusal disposition can decide is decided before the model is asked
anything.** M8's finding was that a refusal is code's job; letting one reach the
model here would quietly put a tier-1 call on turns that used to be free.

MILESTONE M9.
"""

from __future__ import annotations

from dataclasses import dataclass

# Bands. Disposition is an int on the NPC's node, so it outlives the run -- a
# player who attacks someone has done something lasting.
WARY = -1
NEUTRAL = 0
# One meaningful gift crosses it. Items are scarce enough on a floor that
# requiring two means the band is never reached in an ordinary run, and a band
# nobody reaches is the same as not having one.
WARM = 1

# A gift moves disposition by a fixed step, and never by a number the model
# supplied. M3 measured a 1.7b rating everything at the maximum when asked to
# score; letting it price a gift would make disposition free within two turns.
GIFT_STEP = 1
ATTACK_STEP = -3
DISPOSITION_CAP = 4

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "act": {"type": "string", "enum": ["accept", "refuse", "give", "warn", "nothing"]},
        "object": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["act", "object", "reason"],
}


@dataclass(frozen=True)
class Decision:
    act: str
    object: str = ""
    reason: str = ""


def presentable(text: str) -> bool:
    """Is this `reason` fit to be spoken aloud?

    The field exists so the schema has somewhere to put the model's rationale,
    and a 1.7b fills it with fragments -- "accepted for safekeeping", "delver
    requested water, and I have it." Those read as debug output next to prose
    the narrator wrote. Same discipline as every other output guard here: check
    it, and fall back rather than showing the player something broken.
    """
    text = (text or "").strip()
    words = text.split()
    return (
        len(words) >= 3
        and text[0].isupper()
        and text[-1] in ".!?"
    )


def refused(reason: str = "They decline.") -> Decision:
    """What every failure returns.

    A broken call must never be able to hand the player something. Same rule as
    the judge's *implausible*: the failure mode of an exchange is that it does
    not happen.
    """
    return Decision(act="refuse", reason=reason)


# -- disposition -----------------------------------------------------------


def disposition(store, anchor: str) -> int:
    try:
        return int(store.node_data(anchor).get("disposition") or 0)
    except (TypeError, ValueError):
        return 0


def bump(store, anchor: str, delta: int) -> int:
    """Move disposition by a fixed step, clamped.

    The cap is not decoration: without it a player empties their pack into one
    NPC and buys every band, which makes the bands meaningless.
    """
    value = max(-DISPOSITION_CAP, min(DISPOSITION_CAP, disposition(store, anchor) + delta))
    data = store.node_data(anchor)
    data["disposition"] = value
    store.set_node_data(anchor, data)
    return value


def is_wary(store, anchor: str) -> bool:
    return disposition(store, anchor) <= WARY


def is_warm(store, anchor: str) -> bool:
    return disposition(store, anchor) >= WARM


# -- what an NPC is holding ------------------------------------------------


def holdings(store, anchor: str) -> list[dict]:
    data = store.node_data(anchor)
    items = data.get("inventory")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _as_dict(item) -> dict:
    return {"id": item.id, "name": item.name, "heal": item.heal, "damage": item.damage}


def take(store, anchor: str, item) -> None:
    """The NPC now holds this."""
    data = store.node_data(anchor)
    inv = [i for i in (data.get("inventory") or []) if isinstance(i, dict)]
    inv.append(_as_dict(item))
    data["inventory"] = inv
    store.set_node_data(anchor, data)


def release(store, anchor: str, item_id: str) -> dict | None:
    """The NPC no longer holds this. Returns the row, or None if it never did."""
    data = store.node_data(anchor)
    inv = [i for i in (data.get("inventory") or []) if isinstance(i, dict)]
    for i, row in enumerate(inv):
        if row.get("id") == item_id:
            inv.pop(i)
            data["inventory"] = inv
            store.set_node_data(anchor, data)
            return row
    return None


def match(name: str, rows) -> dict | None:
    """Resolve a model-supplied name against things that actually exist.

    The model returns an `object` string. Trusting it lets an NPC hand over
    something that never existed -- permanently, in a world that persists. So a
    name only ever names one of these rows, and anything else is a refusal.
    """
    needle = (name or "").strip().lower()
    if not needle:
        return None
    for row in rows:
        label = str(row.get("name", "")).lower()
        if needle == label or needle in label or label in needle:
            return row
    return None


# -- the call --------------------------------------------------------------


def _ask(messages, client, policy) -> Decision:
    try:
        data = client.structured(messages, DECISION_SCHEMA, policy, kind="tier1")
    except Exception:
        return refused()
    act = str((data or {}).get("act", "")).strip().lower()
    if act not in ("accept", "refuse", "give", "warn", "nothing"):
        return refused()
    return Decision(
        act=act,
        object=str((data or {}).get("object", "")).strip(),
        reason=str((data or {}).get("reason", "")).strip(),
    )


def decide_gift(npc, item, client, policy) -> Decision:
    """Will they take it? Only ever asked once code knows the player holds it."""
    if client is None:
        return Decision(act="accept", object=item.name, reason="")
    return _ask(
        [
            {"role": "system", "content":
                f"You are {npc.name}, who {npc.role}. {npc.voice} "
                f"Decide only whether to take what is offered. Be terse."},
            {"role": "user", "content":
                f"A delver offers you: {item.name}.\n"
                f"Answer 'accept' or 'refuse', and name the object exactly as written."},
        ],
        client, policy,
    )


def decide_request(npc, wanted: str, rows, client, policy) -> Decision:
    """Will they hand it over? Only asked once disposition has allowed it."""
    if client is None:
        return refused()
    have = ", ".join(str(r.get("name", "")) for r in rows) or "nothing"
    return _ask(
        [
            {"role": "system", "content":
                f"You are {npc.name}, who {npc.role}. {npc.voice} "
                f"Decide only whether to hand over one thing you carry. Be terse."},
            {"role": "user", "content":
                f"You carry: {have}.\n"
                f"A delver asks you for: {wanted}.\n"
                f"Answer 'give' or 'refuse'. If you give, name the object exactly "
                f"as it appears in what you carry."},
        ],
        client, policy,
    )
