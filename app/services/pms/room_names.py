"""
Which of this hotel's rooms the guest just named.

**The gap this closes, in the words it was left in.** A comment in the
conversation corpus recorded it as an honest boundary rather than a bug:

    Nothing in this system resolves a room *by name*: "Garden Standard" is a
    string, the session holds uuids, and a guest naming a room in prose reaches
    the session only when the model puts the id into a tool that takes one.

So *"Actually Garden Standard chahiye"* on a turn where the model answered in
prose changed nothing. The session kept the Deluxe, the next prompt told the
model the guest had settled on the Deluxe, and the quote that followed priced
the wrong room.

**Deterministic, and scoped to one hotel's configured rooms.** The resolver
never invents an identifier and never creates a room: it can only ever return
one of the rows it was handed, or nothing. A model asked to produce a uuid will
produce a plausible one, which is the failure this exists to make impossible.

**It matches the hotel's names against the guest's sentence, not the other way
round.** Extracting "the room name" from arbitrary text needs a grammar nobody
has; asking "does this hotel's *Garden Standard* appear in what they wrote" needs
only the room list. Every room is tried, and the answer is one of three:

    resolved     exactly one room is named
    ambiguous    several are equally plausible → ask, never guess
    unresolved   none is → ask, never create

The middle one is the point. A hotel with a *Deluxe Balcony* and a *Deluxe
Garden* has a guest who says "the deluxe one", and the only safe answer to that
is a question.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# Words that appear in half a hotel's room names and distinguish none of them.
# A room called "Deluxe Room" is named by "deluxe", not by "room" — and matching
# on the stop word alone would make every room a candidate for every sentence.
#
# Hinglish included, because a guest writes "deluxe wala kamra" as readily as
# "deluxe room".
_STOP = frozenset({
    "room", "rooms", "kamra", "kamre", "kamara", "the", "a", "an", "our", "your",
    "wala", "wali", "vala", "vali", "type", "category", "option",
})

# How close a token has to be to count as the same word. 0.82 admits "gardern"
# for "garden" and "delux" for "deluxe" — the typos people actually make on a
# phone — and refuses "garden" for "guard".
_TOKEN_SIMILARITY = 0.82

# How far ahead the best room has to be before it is *the* answer rather than a
# coin toss. Two rooms within this of each other is what "ambiguous" means.
_MARGIN = 0.15


def _fold(text: str) -> str:
    """
    Lowercase, strip accents and punctuation, collapse whitespace.

    Devanagari is romanised first, so *"गार्डन स्टैंडर्ड"* and *"Garden
    Standard"* fold to the same tokens. That is the "where practical" half of
    the brief: the transliterator is the one the outbound language rule already
    uses, so a room name it cannot handle degrades to no match — a question for
    the guest — rather than to a wrong match.
    """
    from app.services.messaging import script

    if script.has_devanagari(text):
        text = script.romanize(text)
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> list[str]:
    return [t for t in _fold(text).split() if t]


def _significant(name: str) -> list[str]:
    """
    The tokens that actually identify this room.

    Falls back to every token when a name is nothing but stop words — a hotel
    with a room type literally called "Room" is unusual and still has to be
    matchable.
    """
    words = _tokens(name)
    meaningful = [w for w in words if w not in _STOP]
    return meaningful or words


def _squeeze(word: str) -> str:
    """
    Collapse doubled letters. `gaardan` → `gardan`, `villa` → `vila`.

    **A transliteration artefact, not a threshold fudge.** Devanagari `आ`
    romanises to `aa`, so *"गार्डन"* arrives as `gaardan` and scores 0.769
    against `garden` — below the bar. Squeezed, it is 0.833 and matches, while
    `guard` against `garden` stays at 0.727 and still does not. The doubling is
    the writing system's, not the guest's, and removing it is the honest
    comparison rather than a lower threshold that would let real near-misses
    through.
    """
    return re.sub(r"(.)\1+", r"\1", word)


def _token_hit(token: str, haystack: list[str]) -> float:
    """How well this token is present in the guest's sentence, 0 to 1."""
    best = 0.0
    squeezed = _squeeze(token)
    for word in haystack:
        if word == token:
            return 1.0
        ratio = max(SequenceMatcher(None, token, word).ratio(),
                    SequenceMatcher(None, squeezed, _squeeze(word)).ratio())
        if ratio > best:
            best = ratio
    return best if best >= _TOKEN_SIMILARITY else 0.0


@dataclass(frozen=True)
class Candidate:
    room_type_id: str
    name: str
    score: float


@dataclass(frozen=True)
class Resolution:
    """
    What the guest's sentence named, and how sure we are.

    `ask` is the field callers act on: it is set whenever the honest next move
    is a question rather than a state change, which covers both "several rooms
    match" and "one nearly matches". A near miss is not a match.
    """

    room_type_id: str | None = None
    name: str | None = None
    confidence: str = "none"     # exact · normalised · fuzzy · none
    candidates: tuple[Candidate, ...] = field(default_factory=tuple)
    ask: str | None = None

    @property
    def resolved(self) -> bool:
        return self.room_type_id is not None

    @property
    def ambiguous(self) -> bool:
        return self.room_type_id is None and len(self.candidates) > 1


def _clarify(names: list[str]) -> str:
    """The question to put to the guest, in their own hotel's words."""
    if len(names) == 1:
        return f"Did you mean the {names[0]}?"
    listed = ", ".join(names[:-1]) + f" or the {names[-1]}"
    return f"Which one did you mean — the {listed}?"


def resolve(message: str, rooms: list[dict]) -> Resolution:
    """
    Which configured room the guest named, or a question to ask them.

    `rooms` is this property's own `room_types` rows. Nothing else is consulted
    and nothing outside that list can be returned, which is the whole safety
    property: the resolver cannot invent a room and cannot reach another hotel's.
    """
    if not message or not rooms:
        return Resolution()

    said = _tokens(message)
    if not said:
        return Resolution()

    folded_message = " ".join(said)
    scored: list[Candidate] = []
    exact: list[Candidate] = []

    for room in rooms:
        name = (room.get("name") or "").strip()
        identifier = room.get("id")
        if not name or not identifier:
            continue

        folded_name = _fold(name)
        if folded_name and folded_name in folded_message:
            # The hotel's own words, verbatim, inside the guest's sentence.
            exact.append(Candidate(str(identifier), name, 1.0))
            continue

        wanted = _significant(name)
        if not wanted:
            continue
        hits = [_token_hit(token, said) for token in wanted]
        if any(h == 0.0 for h in hits):
            # **Every significant token has to be there.** A guest who writes
            # "I saw a garden" has not named the Garden Standard, and a rule
            # that matched on one token would take a passing noun for a room
            # selection.
            continue
        scored.append(Candidate(str(identifier), name, sum(hits) / len(hits)))

    if len(exact) == 1:
        return Resolution(exact[0].room_type_id, exact[0].name, "exact", tuple(exact))
    if len(exact) > 1:
        # Two configured names both appear. "Garden Standard" is inside "Garden
        # Standard Twin", so a hotel that sells both has a genuinely ambiguous
        # sentence — the longer name is not automatically the intended one.
        best = max(exact, key=lambda c: len(c.name))
        rest = [c for c in exact if c is not best]
        if all(len(best.name) > len(c.name) + 2 for c in rest):
            return Resolution(best.room_type_id, best.name, "exact", tuple(exact))
        names = [c.name for c in exact]
        return Resolution(candidates=tuple(exact), ask=_clarify(names))

    if not scored:
        # **Nothing matched in full. Several matching in *part* is still a
        # question worth asking.**
        #
        # "the deluxe one" at a hotel with a Deluxe Balcony and a Deluxe Garden
        # names neither and means one of them, and that is exactly the case the
        # brief says never to guess at.
        #
        # Only when two or more rooms share the partial hit. One room matching
        # one token is how "I saw a garden" would become an interrogation about
        # the Garden Standard — so a lone partial match stays silent and the
        # model answers conversationally, as it did before this existed.
        partial = []
        for room in rooms:
            name, identifier = (room.get("name") or "").strip(), room.get("id")
            if not name or not identifier:
                continue
            wanted = _significant(name)
            hits = [_token_hit(token, said) for token in wanted]
            if wanted and any(h > 0 for h in hits):
                partial.append(Candidate(str(identifier), name,
                                         sum(hits) / len(wanted)))
        if len(partial) > 1:
            return Resolution(candidates=tuple(partial),
                              ask=_clarify([c.name for c in partial]))
        return Resolution()

    scored.sort(key=lambda c: c.score, reverse=True)
    best = scored[0]
    contenders = [c for c in scored if best.score - c.score < _MARGIN]
    if len(contenders) > 1:
        return Resolution(candidates=tuple(contenders),
                          ask=_clarify([c.name for c in contenders]))

    # **A name whose distinguishing words are also another room's is not
    # distinguishing.**
    #
    # One real property in this database sells `deluxe room` and `deluxe king
    # bed`. "room" is a stop word, so the first reduces to the single
    # significant token `deluxe` — and a guest writing "deluxe chahiye" matched
    # it completely, scored 1.0, and was sold `deluxe room` with no question
    # asked. The second room needs three tokens and never entered the scoring at
    # all, so there was no contender to trigger the margin rule.
    #
    # It is the exact situation the `exact` branch above already handles for
    # "Garden Standard" inside "Garden Standard Twin" — the shorter name is not
    # automatically the intended one — and the scored branch had no equivalent.
    # The guest said a word that is true of two rooms. They have named neither.
    #
    # Only when the *other* room's significant tokens are a strict superset: a
    # room whose name merely shares an adjective is already handled by the
    # margin, and widening this further would start asking about "Pool Villa"
    # because some other room happens to contain the word pool.
    wanted_best = set(_significant(best.name))
    also_true_of = [
        room.get("name") for room in rooms
        if str(room.get("id")) != best.room_type_id and room.get("name")
        and wanted_best and wanted_best < set(_significant(room["name"]))
    ]
    if also_true_of:
        return Resolution(
            candidates=tuple(scored[:3]),
            ask=_clarify([best.name, *(n for n in also_true_of if n)]))

    return Resolution(best.room_type_id, best.name, "fuzzy", tuple(scored[:3]))


# ── the hotel's room list, cached briefly ───────────────────────────────────
#
# Resolution needs the configured names on every guest turn, and reading them
# every turn is a small indexed query against a table with a handful of rows.
# Cached anyway, because "a handful of rows" is a fact about today's hotels and
# the read is on the path of every message.
#
# **Sixty seconds, not forever.** A hotel adds a room type rarely, but it does
# add them, and a process-lifetime cache means the agent cannot sell a new room
# until somebody restarts the worker. A minute is short enough that nobody
# notices and long enough that a busy thread reads once.
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_TTL_SECONDS = 60.0


async def for_property(property_id: str) -> list[dict]:
    """This hotel's sellable room types, as `{id, name}` rows."""
    import time

    from app.db.client import run_db

    cached = _CACHE.get(property_id)
    now = time.monotonic()
    if cached and now - cached[0] < _TTL_SECONDS:
        return cached[1]

    def _read():
        from app.db.client import get_db

        return (
            get_db().table("room_types").select("id,name")
            .eq("property_id", property_id).eq("is_active", True)
            .execute().data or []
        )

    try:
        rooms = await run_db(_read)
    except Exception:  # noqa: BLE001
        # A resolver that cannot read the room list resolves nothing, which is
        # the behaviour before it existed. It must never be the reason a turn
        # fails.
        import logging

        logging.getLogger(__name__).warning(
            "could not read room types for %s", property_id, exc_info=True)
        return cached[1] if cached else []

    _CACHE[property_id] = (now, rooms)
    return rooms
