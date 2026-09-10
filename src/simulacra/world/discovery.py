"""Is that thing here? Pure code, no model, no I/O.

The obvious design for progressive discovery is to ask the model whether a thing
is plausible in this room. That puts a tier-1 call on `look` -- the one verb
documented as costing nothing -- and asks a 1.7b a question it has no grounds to
answer.

The better source was already sitting there. The director has been writing
concept lines like *"There is a cracked altar and a single glowing crystal"*
since M2, and the narrator expands them into prose the player reads. Then
`look at the altar` answered *"You don't see altar here."* The room had told the
player what was in it and the engine disagreed.

So plausibility is graded here, for free, in two grades:

* **mentioned** -- the word is in the room's own concept, prose or name. Always
  found. This is the feature: the nouns the player actually read work.
* **fixture** -- the word is in the theme's pool for this kind of room. Found if
  the room is fertile, which is rolled **once per room** rather than per look:
  rolling per attempt lets a player type the same thing four times until it
  lands, which is the grind the budget exists to prevent.

Everything else is absent, and says so.

MILESTONE M10.
"""

from __future__ import annotations

import random
import re

# A room yields this many discoveries and is then finished. Code owns how many;
# the model owns what. Without a cap a player grinds unlimited content out of one
# room and the dungeon stops meaning anything.
DISCOVERY_BUDGET = 3

# Roughly the fraction of rooms that hold something beyond what was written into
# their concept. Rolled per room, from the world seed, so it is a property of the
# place rather than of how many times you asked.
FERTILITY = 0.5

# Short words match everything. Two-letter targets are not searches.
MIN_TARGET = 3

_WORD = re.compile(r"[a-z']+")

# Never a thing, however often the prose uses them. Measured live: `look at the
# under` found "a patch of moss and dust", because a preposition was passing the
# minimum length and appearing in the room's own text.
#
# This catches function words. It does not catch *verbs* -- `look at the groans`
# still finds something -- and short of part-of-speech tagging it will not; a
# regex cannot tell a noun from a verb, and guessing wrong in either direction
# is worse than the odd strange answer to strange input.
_NOISE = frozenset({
    # articles, conjunctions, pronouns
    "the", "a", "an", "and", "or", "but", "nor", "so", "yet",
    "it", "its", "this", "that", "these", "those", "there", "here",
    "you", "your", "yours", "they", "them", "their", "he", "she", "his", "her",
    "who", "whom", "whose", "which", "what", "some", "any", "each", "every",
    # prepositions
    "of", "in", "on", "at", "to", "with", "for", "from", "by", "as", "into",
    "onto", "upon", "under", "over", "above", "below", "beneath", "behind",
    "before", "after", "across", "through", "toward", "towards", "against",
    "between", "among", "around", "about", "off", "out", "up", "down", "near",
    # auxiliaries and copulas
    "is", "are", "was", "were", "be", "been", "being", "am",
    "has", "have", "had", "do", "does", "did", "will", "would", "can", "could",
    "shall", "should", "may", "might", "must",
    # degree and time adverbs that read like things but never are
    "very", "more", "most", "less", "least", "much", "many", "still", "again",
    "once", "never", "always", "then", "than", "now", "just", "only", "not",
})


def words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _NOISE}


def _target_words(target: str) -> set[str]:
    return {w for w in words(target) if len(w) >= MIN_TARGET}


def is_fertile(room_id: str, world_seed: int) -> bool:
    """Does this room hold anything beyond what its concept named?

    Seeded from the room and the world so the answer is the same every time it
    is asked, this run and every later one.
    """
    return random.Random(f"fertile:{world_seed}:{room_id}").random() < FERTILITY


def grade(target: str, room, theme, *, world_seed: int = 0) -> str:
    """"mentioned" | "fixture" | "absent". No store, no model, no I/O."""
    wanted = _target_words(target)
    if not wanted:
        return "absent"

    # What the player has actually read: the director's concept, the narrator's
    # prose, and the room's name. Grading against the concept alone would miss
    # the nouns the prose introduced, which are the ones they saw.
    said = words(room.concept) | words(room.prose) | words(room.name)
    if wanted & said:
        return "mentioned"

    pool: set[str] = set()
    for name in theme.fixture_names(room.kind):
        pool |= words(name)
    if wanted & pool and is_fertile(room.id, world_seed):
        return "fixture"

    return "absent"
