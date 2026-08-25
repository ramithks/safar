"""
The business actions a message *requires*, decided without asking the model.

**The failure this exists for.** The same conversation, replayed against the
same build with the same model, scored between 28 and 44 of 44 assertions. The
two that moved were always the same two:

    "Actually 5 kamre chahiye, 10 log aa rahe hain"   → sometimes no get_group_rates
    "Yaar thoda kam karo na, 4000 per night mein?"    → sometimes no negotiate_rate

Nothing was wrong with the code on the runs that failed. The tool was granted,
it was on the menu, its description says to use it, and the prompt says so
twice. The model simply answered instead — *"ji bilkul, main check karti hoon"*
— and on the negotiation turn the reply then named the guest's own figure, which
the reply guardrail correctly refused, which sent a guest who was ready to pay
to a person who was asleep.

That is not a prompt bug. **A business action that a hotel's revenue depends on
cannot be a thing the model does when it feels like it.** The vocabulary that
recognises "5 kamre chahiye" is a hundred lines of regex, it is already written
next door in `messaging/obligations.py` for a different purpose, and it is right
every time.

**So this module answers one question: given the guest's words and where the
conversation has got to, which tool MUST run this turn?** It does not answer
the question the model is good at — how to say the result, what else to ask,
whether the guest sounds annoyed. `runtime.run_turn` gives the model its turn
first and only steps in when the required call did not happen; see
`runtime._enforce`.

Three rules keep this from becoming a second, worse agent:

  * **It only ever adds a tool call the model was already allowed to make.**
    Forcing goes through `registry.execute` like everything else — permission,
    autonomy, argument validation, the property's own rules. A level-2 agent
    that may not negotiate still may not negotiate; it gets a queued approval,
    which is the same answer it would have got had the model called the tool
    itself. Nothing here can widen anything.

  * **It never invents an argument.** Every field comes from the guest's own
    sentence or from `conversations.guest_profile`, which is itself derived from
    tool results and never from prose (see `messaging/session.py`). When the
    context to build a safe call is missing — a negotiation before anyone has
    named a rate plan — the requirement is recorded as unmet and the model's own
    reply stands, because a turn that asks "which room did you have in mind?"
    is the correct answer to that situation.

  * **The result goes back to the model to say.** A forced tool produces a
    payload and an instruction, exactly as a model-initiated one does, and the
    model writes the sentence. The alternative — a canned reply — would be in
    the wrong language and the wrong voice, and this product's whole claim is
    that it does not sound like a form.

**Detection is deliberately conservative in one direction and not the other.**
A missed requirement leaves the system exactly as it was before this module
existed. A false one calls a read-only tool nobody needed (`get_group_rates` for
a couple) or, worse, opens a negotiation the guest never asked for. So the
group vocabulary needs a room-or-people noun beside its number, and the
negotiation vocabulary needs a word that only appears when somebody is pushing
on a price.
"""
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger(__name__)


def enforcement_enabled() -> bool:
    """
    Whether the application runs a required tool the model skipped.

    A switch rather than a constant, for two reasons and neither of them is
    doubt about the feature.

    **It is how the improvement is measured.** `scripts/agent_eval.py` runs the
    same scenarios with it on and off against the live model; the difference
    between those two numbers is the only honest statement of what this layer
    bought, and a hard-coded `True` would make that comparison require a code
    edit and a redeploy.

    **And it is the rollback.** If forcing ever turns out to be wrong for one
    property — a hotel that wants its agent to ask before it prices anything —
    the answer is an environment variable, not a hotfix at eleven at night.
    Detection keeps running either way, so `agent_runs.required_actions` still
    records what *would* have been required and the report still counts it.

    Read per call rather than cached at import, so a test can flip it and so
    the eval runner can flip it between sweeps in one process.
    """
    return os.environ.get("AGENT_FORCE_REQUIRED_ACTIONS", "true").strip().lower() not in (
        "false", "0", "no", "off",
    )

# ── the intents ──────────────────────────────────────────────────────────────
#
# Named for what the guest is doing, not for the tool that answers it: several
# of these are answered by different tools depending on where the conversation
# has got to, and an intent named after a tool would have to be renamed the
# first time that changed.

GROUP_BOOKING = "GROUP_BOOKING"
NEGOTIATION = "NEGOTIATION"
QUOTE_REQUIRED = "QUOTE_REQUIRED"
PAYMENT = "PAYMENT"
BOOKING_STATUS = "BOOKING_STATUS"
BOOKING_CANCELLATION = "BOOKING_CANCELLATION"
BOOKING_MODIFICATION = "BOOKING_MODIFICATION"
ESCALATION = "ESCALATION"
ROOM_SELECTION = "ROOM_SELECTION"
DATE_CHANGE = "DATE_CHANGE"
GUEST_COUNT_CHANGE = "GUEST_COUNT_CHANGE"

INTENTS = (
    GROUP_BOOKING, NEGOTIATION, QUOTE_REQUIRED, PAYMENT, BOOKING_STATUS,
    BOOKING_CANCELLATION, BOOKING_MODIFICATION, ESCALATION, ROOM_SELECTION,
    DATE_CHANGE, GUEST_COUNT_CHANGE,
)


@dataclass(frozen=True)
class Requirement:
    """
    One business action this turn owes the guest.

    `satisfied_by` is wider than `tool` on purpose. A guest asking for five
    rooms is answered by `get_group_rates`, but a model that went straight to
    `get_quote` with `units=5` has done the *right* thing — the quote applies
    the band itself — and forcing a second call after it would be the system
    talking over a model that was already correct.
    """

    intent: str
    tool: str
    satisfied_by: frozenset[str]
    # Whether the application runs `tool` itself when the model did not. False
    # means "recorded, and watched in the evaluation report" — which is what an
    # intent whose arguments cannot be built safely gets.
    must_execute: bool
    # Ready-to-run arguments, or None when the context was not there. Never
    # partially filled: a call with a guessed date is worse than no call.
    arguments: dict | None = None
    # Why, for `agent_runs.required_action_status` and for a human reading it.
    detail: str = ""
    # What the guest's own words carried, kept for the session and the report.
    evidence: dict = field(default_factory=dict)

    @property
    def forceable(self) -> bool:
        return self.must_execute and self.arguments is not None


# ── numbers, as guests write them ────────────────────────────────────────────
#
# "5 kamre", "paanch kamre", "five rooms", "5 rooms chahiye" are one request.
# Digits alone would have covered the replay's own sentence and missed a third
# of the transcripts, where the number is a word.

_NUMBER_WORDS = {
    # English
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    # Hinglish, in the spellings that actually turn up
    "ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4,
    "panch": 5, "paanch": 5, "chhe": 6, "che": 6, "chah": 6, "chhah": 6,
    "saat": 7, "sat": 7, "aath": 8, "ath": 8, "nau": 9, "das": 10, "dus": 10,
    "gyarah": 11, "barah": 12, "pandrah": 15, "bees": 20, "bis": 20,
    "pachas": 50,
    # Hindi
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6,
    "सात": 7, "आठ": 8, "नौ": 9, "दस": 10, "बीस": 20,
}

_NUMBER = r"(?:\d{1,3}|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"

# The nouns that make a number a number *of rooms*.
_ROOM_NOUN = r"(?:rooms?|kamre|kamra|kamrey|kamron|कमरे|कमरा|suites?|villas?|cottages?|tents?)"
# …and the ones that make it a number of people. Guests state party size at
# least as often as room count, and a hotel that hears "10 log" and does not
# reach for its group bands has misread the largest enquiry of its week.
_PEOPLE_NOUN = (
    r"(?:people|persons?|pax|guests?|adults?|heads?|members?|"
    r"log|logo|logon|jane|bande|लोग|व्यक्ति|मेहमान)"
)

# **A room's name can sit between the number and the noun.**
#
# "2 Deluxe rooms", "3 Garden Standard rooms", "5 sea view kamre" — an entirely
# ordinary way to ask, and the pattern required the number to touch the noun, so
# every one of them counted no rooms at all. Found by a long journey whose very
# first turn was "Hi, 2 Deluxe rooms chahiye".
#
# Up to three words between, and none of them a digit: `{_NUMBER}` would let
# "2 rooms for 4 people" match "2 … people" and count four rooms, which is the
# failure `_ROOMS_AFTER`'s guard already exists for.
_ROOMS_BEFORE = re.compile(
    rf"(?<![a-z0-9])({_NUMBER})\s*(?:[a-z]+\s+){{0,3}}{_ROOM_NOUN}", re.IGNORECASE)
# The number *after* the noun — "rooms 4 chahiye", "kamre 3".
#
# With a guard that earns its keep: "rooms for 10 people" put ten rooms on a
# request for five, because the pattern read the number beside the room noun and
# never looked at what the number was counting. A group tool called with double
# the rooms quotes a band the guest does not qualify for.
_ROOMS_AFTER = re.compile(
    rf"{_ROOM_NOUN}\s*(?:ki|ke|for)?\s*({_NUMBER})(?![a-z0-9])(?!\s*{_PEOPLE_NOUN})",
    re.IGNORECASE,
)

# **"room 204" is a door, not an order for two hundred rooms.**
#
# `_ROOMS_AFTER` reads the number beside a room noun, which is right for "kamre
# 3 chahiye" and catastrophic for "room 204 mein AC nahi chal raha" — a guest
# reporting a broken air conditioner raised a GROUP_BOOKING requirement and
# forced `get_group_rates(units=204)`.
#
# Nobody books three figures of rooms through a chat window, and every hotel
# numbers its doors that way, so the number itself decides. Applied only to the
# noun-first form: "204 rooms chahiye" is at least stated as a count, and is
# left alone rather than second-guessed.
_ROOM_NUMBER_FROM = 100

# **A door, named as one.** The brief's own example:
#
#     "room 204 mein AC nahi chal raha"   → room_number 204, a complaint
#     "204 wala room"                     → room_number 204
#     "204 rooms chahiye"                 → room_count 204
#
# The first two used to read as a *count*, and the second still did after the
# last pass widened the count pattern to allow a room name between the number
# and the noun ("2 Deluxe rooms"). "204 wala room" fits that shape exactly.
#
# Refusing to count them was half the job. The other half is capturing what the
# number actually is, because `create_task` takes a `room_number` and a guest
# reporting a broken air conditioner has just told us which door.
#
# Two shapes, and both put the number beside the noun rather than in front of a
# quantity: `room 204` and `204 wala room`. A bare number with no room noun
# anywhere near it is not a room number either — that is a price, a year, or a
# phone.
_ROOM_NUMBER = re.compile(
    rf"(?<![a-z0-9])(?:{_ROOM_NOUN}\s*(?:no\.?|number|#)?\s*(\d{{1,4}})"
    rf"|(\d{{1,4}})\s*(?:wala|wale|wali|vala|vale)\s*{_ROOM_NOUN})"
    r"(?![a-z0-9])",
    re.IGNORECASE,
)


def room_number(message: str) -> str | None:
    """
    The door the guest is talking about, or None.

    Returned as a string because a room number is a label, not a quantity: a
    hotel with rooms "04" and "4" has two different doors, and `int` would make
    them one.
    """
    for match in _ROOM_NUMBER.finditer(message or ""):
        raw = match.group(1) or match.group(2)
        if not raw:
            continue
        # A leading zero settles it whatever the magnitude: "room 04" is a
        # label and nobody has ever written "04 rooms". Without this the
        # threshold below rejects it, and a hotel numbering its ground floor
        # 01–09 loses every complaint from it.
        if raw.startswith("0") and len(raw) > 1:
            return raw
        # Otherwise the threshold. Below it the reading is ambiguous with a
        # count — "room 5" is as likely to be five rooms as door five — and
        # `room_count` already claims those. Above it, no hotel is being asked
        # for that many rooms in a chat window.
        if int(raw) >= _ROOM_NUMBER_FROM:
            return raw
    return None
_PEOPLE_BEFORE = re.compile(rf"(?<![a-z0-9])({_NUMBER})\s*{_PEOPLE_NOUN}", re.IGNORECASE)
_PEOPLE_AFTER = re.compile(rf"{_PEOPLE_NOUN}\s*(?:hain?|hai|ke liye|for)?\s*({_NUMBER})(?![a-z0-9])",
                           re.IGNORECASE)

# **"there are 12 of us", "we're 10 of us", "12 of us are coming".**
#
# The commonest way an English-speaking group says its size, and it names no
# noun at all — so both patterns above, which are built around a word for
# people, miss it entirely. A twelve-person enquiry raised no requirement and
# reached no group pricing.
#
# Found by a long journey rather than a unit test: the guest opened with "we're
# 10 people" (which was detected) and then corrected to "actually 12 of us now"
# (which was not), so the band silently stopped following the party.
#
# `of us` is the whole signal, and it is unambiguous above one — there is no
# other thing a hotel guest counts that way.
#
# **`one of us` is excluded, and it is not a rounding error.** It is idiomatic
# English for "a member of our group" — *"one of us is vegetarian"*, *"one of us
# arrives late"* — and reading it as a party of one would overwrite a party of
# twelve on the session with a 1. A guest who really is alone says "just me" or
# "1 adult", never "1 of us".
_PEOPLE_OF_US = re.compile(rf"(?<![a-z0-9])({_NUMBER})\s+of\s+us(?![a-z0-9])",
                           re.IGNORECASE)


def _of_us(text: str) -> int | None:
    for match in _PEOPLE_OF_US.finditer(text or ""):
        value = _to_int(match.group(1))
        if value and value > 1:
            return value
    return None

# "group booking", "shaadi", "bulk" — a group with no number in the sentence.
# It still requires the tool, because the answer is the hotel's bands and its
# minimum, neither of which the model may state from memory.
_GROUP_WORDS = re.compile(
    r"(?<![a-z0-9])(?:group booking|group rate|group rates|group|bulk|"
    r"block booking|multiple rooms|several rooms|few rooms|couple of rooms|"
    r"kai kamre|kai room|corporate|conference|wedding|shaadi|baraat|"
    r"team outing|offsite|reunion)(?![a-z0-9])",
    re.IGNORECASE,
)
_GROUP_WORDS_HI = re.compile(r"समूह|शादी|बारात")

# How many rooms a party of N needs, when the guest gave people and not rooms.
#
# Two to a room is what every hotel in this segment assumes and what the group
# bands are written against. It is an *estimate*, and it is only ever used to
# decide whether to look the bands up — never to price anything, because
# `get_group_rates` prices nothing and `get_quote` takes the real number.
PEOPLE_PER_ROOM = 2

# Below this a "group" is a couple with a spare room, and asking the pricing
# engine for volume bands would be a tool call nobody needed. `get_group_rates`
# itself decides what counts as a group for the property; this only decides
# whether the question is worth asking.
GROUP_FROM_ROOMS = 2
# A party this size is worth checking the bands for even when nobody has said
# the word "rooms" — "hum 6 log hain" is a group enquiry in every hotel in India.
GROUP_FROM_PEOPLE = 4


def _to_int(token: str) -> int | None:
    token = (token or "").strip().lower()
    if token.isdigit():
        value = int(token)
        # A four-digit "number of rooms" is a price, a year or a pin code.
        return value if 1 <= value <= 500 else None
    return _NUMBER_WORDS.get(token)


def room_count(message: str) -> int | None:
    """
    How many rooms this message asks for, or None.

    Rooms first and people second: a guest who says "5 kamre chahiye, 10 log aa
    rahe hain" has told us both, and the number they chose is the one to use.
    """
    text = message or ""
    # A door is not a quantity. "204 wala room" reads as a count to the pattern
    # below — a number, then a word, then a room noun, which is the shape of
    # "2 Deluxe rooms" — so the room-number reading is settled first.
    door = room_number(text)
    for pattern in (_ROOMS_BEFORE, _ROOMS_AFTER):
        for match in pattern.finditer(text):
            value = _to_int(match.group(1))
            if not value:
                continue
            # Compared as numbers, not as strings: the door may be written
            # "04" and the count pattern reads it as 4, so a string comparison
            # lets the same digits be both a label and a quantity.
            if door and value == int(door):
                continue
            if pattern is _ROOMS_AFTER and value >= _ROOM_NUMBER_FROM:
                # A door, not an order. See `_ROOM_NUMBER_FROM`.
                continue
            return value
    return None


def people_count(message: str) -> int | None:
    """How many people this message mentions, or None."""
    text = message or ""
    for pattern in (_PEOPLE_BEFORE, _PEOPLE_AFTER):
        for match in pattern.finditer(text):
            value = _to_int(match.group(1))
            if value:
                return value
    return _of_us(text)


# ── negotiation, as guests write it ──────────────────────────────────────────
#
# Stricter than the obligations vocabulary next door, and for a different job.
# There, a false positive costs one unnecessary sentence in a prompt. Here it
# opens a negotiation — a hold, a discount the hotel did not intend to offer,
# a guest told about a "best rate" they had not asked for. So every pattern
# below needs a word that is about *lowering a price*, not merely about price.

_NEGOTIATION = (
    re.compile(
        r"(?<![a-z0-9])(?:discount|concession|rebate|"
        r"best (?:price|rate|deal|offer)|"
        r"last (?:price|rate)|final (?:price|rate|rate batao)|lowest (?:price|rate)|"
        r"cheaper|cheapest|budget mein|expensive|"
        # Found by a paraphrase sweep across thirty ways guests ask for the
        # same thing — see tests/test_haggle_vocabulary.py. Each of these was
        # a miss, and a miss is a turn where the pricing engine is never asked
        # and the model answers a price question out of its own head.
        #
        # `mehenga` is the same word as `mehnga` with the vowel a guest happens
        # to type; `deal` needs a qualifier because "deal" alone appears in
        # "great deal of noise"; "do better" and "kuch aur karo" are the two
        # commonest ways to push without naming a number.
        r"mehenga|mahenga|mahanga|mahnga|"
        r"(?:any|koi|kuch|good|better|best) deal|deal (?:do|dijiye|karo)|"
        # A rate *class* is a discount ask by another name. "Corporate rate?"
        # is not a question about the public price — it is a request to be
        # charged a different one, and it has to reach the engine that decides
        # whether this property has such a rate at all.
        r"(?:corporate|company|business|agent|trade|staff|govt|government|"
        r"senior citizen|student|member|loyalty|repeat guest) (?:rate|price|tariff|discount)|"
        r"do better|kar sakte ho kya|kuch aur karo|kuch to karo|kuch toh karo|"
        r"kam kar|kam karo|kam kardo|kam karna|kam ho jaye|kam hoga|kam kijiye|"
        r"kuch kam|thoda kam|aur kam|zyada hai|jyada hai|mehnga|mehanga|"
        r"sasta|saste|kam se kam|negotiat\w*|"
        r"koi offer|kuch offer|any offer|special offer|offer chal|"
        r"special rate|special price|"
        # **`kar do` alone was here and it is simply "do it".**
        #
        # Measured against the ways guests actually use it: "book kar do",
        # "cancel kar do", "confirm kar do", "check in 2 baje kar do",
        # "breakfast add kar do" and a date change written as "4 tareekh se 6
        # tareekh kar do" were every one of them read as a haggle. Each opens a
        # negotiation, takes a hold, and can volunteer a discount to a guest who
        # never asked for one.
        #
        # Nothing is lost by removing it, which is the test of a pattern this
        # loose: every genuine haggle in that set is already matched by a word
        # that is actually about lowering a price — `kam kar` for "kam kar do",
        # `discount` for "discount kar do", and `_OFFERED_PRICE_BEFORE_VERB`
        # for "4000 kar do". The bare verb was carrying only false positives.
        #
        # Found by tests/test_conversation_corpus.py, where a guest changing
        # their dates triggered a negotiation and the negotiation then wrote
        # the *old* dates back over the new ones.
        r"adjust kar)(?![a-z0-9])",
        re.IGNORECASE,
    ),
    re.compile(r"कम कर|छूट|सस्ता|महंगा|कम से कम|आखिरी (?:रेट|दाम)"),
    # **A one-word message.**
    #
    # "Final?" and "Lowest?" are haggles, and they are the two that a
    # vocabulary of phrases cannot hold: on their own they are ambiguous
    # enough that matching them anywhere in a sentence would open a
    # negotiation on "what is the final check-out time". As the *whole*
    # message, after a price has been quoted, they mean one thing only.
    re.compile(r"^\s*(?:final|lowest|last|best|minimum|min)\s*[?.!]*\s*$",
               re.IGNORECASE),
    # **"give me 70% off".**
    #
    # Found in production traffic on 2026-08-24, not here: a real guest turn
    # that arrived through the webhook, ran on the live model, and was recorded
    # with `required_action_status: none` — no intent, no requirement, nothing
    # for any deterministic layer to have an opinion about.
    #
    # The vocabulary above is built out of *words* — "discount", "kam karo",
    # "best price" — and a percentage is not a word. It is also one of the two
    # or three commonest ways a guest asks, in every language this product sees,
    # and it was the one shape the list could not express.
    #
    # A digit-plus-`%` needs a discount word beside it, because "70% occupancy"
    # and "18% GST" are not haggles. `off`, `discount`, `kam`, `chhoot` and
    # their neighbours are; a bare percentage is left alone.
    re.compile(
        r"\d{1,2}\s*(?:%|percent|pct|फ़ीसदी|फीसदी)\s*"
        r"(?:ki\s+|ka\s+)?(?:off|discount|kam|kum|chhoot|chhut|छूट|कम)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:off|discount|kam|kum|chhoot|छूट|कम)\s*(?:kar\w*|do|de\s*do|dijiye)?\s*"
        r"\d{1,2}\s*(?:%|percent|pct)",
        re.IGNORECASE,
    ),
)

# "4000 mein ho jayega?", "₹4,000 mein kar do", "3500 tak", "4k mein".
#
# The figure is captured so it can be passed to the engine as the guest's own
# ask — which is the *only* way a guest-supplied number is allowed to travel
# anywhere in this system. It becomes an input to a decision, never an output;
# `negotiate_rate` decides what may be said and the guardrail checks that only
# what it decided was said. See §20 of the brief and `hotel.negotiate_rate`.
_AMOUNT = r"(\d{1,3}(?:,\d{3})+|\d{3,6})"

# A number followed by a budget postposition: "4000 mein", "3500 tak".
_OFFERED_PRICE = re.compile(
    r"(?:₹|rs\.?|inr)?\s*" + _AMOUNT + r"\s*"
    r"(?:rs\.?|rupees|rupaye|/-)?\s*"
    r"(?:mein|me|main|mai|m|tak|pe|par|ke andar|budget|"
    r"per night|pn|/night|a night|nightly|प्रति रात|में|तक)"
    r"(?![a-z0-9])",
    re.IGNORECASE,
)
# The other order: "kar do 4000", "make it 3500", "can you do 4000".
_OFFERED_PRICE_AFTER = re.compile(
    r"(?:kar do|kardo|kar dijiye|make it|do it for|kar sakte ho|"
    r"can you do|could you do|how about|what about|at|for|kar dena|"
    # "budget 5000 hai" — the noun leads and the figure follows, which is the
    # other half of the postposition list below and was missing from both.
    r"budget|budget is|budget hai)\s*"
    r"(?:₹|rs\.?|inr)?\s*" + _AMOUNT + r"(?![a-z0-9])",
    re.IGNORECASE,
)

# And the figure leading its own verb: "3500 kar do", "4000 kar dijiye".
#
# Hindi is verb-final, so this is the *commoner* of the two orders and the
# pattern above only had the English one. Found by the paraphrase sweep.
_OFFERED_PRICE_BEFORE_VERB = re.compile(
    r"(?:₹|rs\.?|inr)?\s*" + _AMOUNT + r"\s*"
    r"(?:rs\.?|rupees|rupaye)?\s*"
    r"(?:kar\s*do|kardo|kar\s*dijiye|kar\s*dena|kar\s*sakte|lagao|laga do)"
    r"(?![a-z0-9])",
    re.IGNORECASE,
)

# **A number a guest marked as money, with no other grammar around it.**
#
# "₹4000 possible?", "4,000 rupees?", "Rs 4000?" — six of twelve ways of naming
# a price were missed before this, because every pattern above needs a
# postposition or a verb next to the figure. A currency marker is a stronger
# signal than either: nobody writes ₹ about a room number.
#
# Deliberately not a bare number. `\d{3,6}` on its own is a year, a room, a
# pincode and a phone fragment, and passing one of those to the pricing engine
# produces a verdict about a stay nobody proposed.
_OFFERED_PRICE_MARKED = re.compile(
    r"(?:(?:₹|rs\.?|inr)\s*" + _AMOUNT + r"|"
    + _AMOUNT + r"\s*(?:₹|rs\b|rupees|rupaye|rupay))(?![a-z0-9])",
    re.IGNORECASE,
)

# "4k", "4.5k" — a thousands shorthand, which is how a guest types a round
# number fastest and was the single commonest miss in the sweep.
_OFFERED_PRICE_K = re.compile(r"(?<![a-z0-9])(?:₹|rs\.?|inr)?\s*(\d{1,3})\s*k(?![a-z0-9])",
                              re.IGNORECASE)


# Words that make the number after them a place or a label rather than money.
_NOT_MONEY_BEFORE = re.compile(
    r"(?:room|rooms|kamra|kamre|kamara|suite|flat|floor|manzil|"
    r"no\.?|number|nmbr|pincode|pin|zip|otp|ref|booking|gst|phone|mobile|"
    r"whatsapp|call)\s*(?:no\.?|number|#)?\s*$",
    re.IGNORECASE,
)


def offered_price(message: str) -> int | None:
    """
    The figure the guest named, in whole rupees, or None.

    Bounded at both ends because the pattern is loose by design: under ₹100 is
    a room number or a count, over ₹10,00,000 a night is a typo, and passing
    either to the engine would produce a verdict about a stay nobody proposed.
    """
    for pattern in (_OFFERED_PRICE, _OFFERED_PRICE_AFTER, _OFFERED_PRICE_BEFORE_VERB,
                    _OFFERED_PRICE_MARKED, _OFFERED_PRICE_K):
        match = pattern.search(message or "")
        if not match:
            continue
        raw = next((g for g in match.groups() if g), None)
        if raw is None:
            continue
        try:
            value = int(raw.replace(",", ""))
        except ValueError:
            continue
        if pattern is _OFFERED_PRICE_K:
            value *= 1000

        # **A room number is not an offer.**
        #
        # "room 204 mein hain" read as ₹204, because `\d{3,6}` followed by
        # `mein` is how a guest names a budget and also how they say which door
        # they are behind. A three-digit number is the commonest room number in
        # this product's hotels and comfortably inside the ₹100 floor.
        #
        # Read from the words in front of the figure rather than from a
        # lookbehind, because the words are different lengths and Python's
        # lookbehind is fixed-width — stacking six of them would be the same
        # rule written unreadably.
        before = (message or "")[max(0, match.start() - 16):match.start()].lower()
        if _NOT_MONEY_BEFORE.search(before):
            continue

        # **A year is not an offer**, and "2026 mein" is the shape of both.
        #
        # Surfaced by the paraphrase sweep, not by a transcript: *"2026 mein aa
        # rahe hain"* — we are coming in 2026 — read as an offer of ₹2,026 and
        # opened a negotiation, because `\d{3,6}` followed by `mein` is exactly
        # how a guest names a budget and exactly how they name a year.
        #
        # Nothing about the number itself separates them; ₹2,026 a night is a
        # perfectly ordinary rate. What separates them is that a guest naming a
        # *price* in the next decade's range says so — with a rupee sign, or
        # with a haggling word beside it. Narrow on purpose: only 2020–2035, and
        # only when neither marker is present.
        if 2020 <= value <= 2035 and pattern in (_OFFERED_PRICE, _OFFERED_PRICE_AFTER):
            marked = _OFFERED_PRICE_MARKED.search(message or "")
            haggling = any(p.search(message or "") for p in _NEGOTIATION)
            if not marked and not haggling:
                continue

        if 100 <= value <= 1_000_000:
            return value
    return None


def wants_discount(message: str) -> bool:
    """Is the guest pushing on the price?"""
    text = message or ""
    if any(pattern.search(text) for pattern in _NEGOTIATION):
        return True
    # A bare figure with a rate word is a haggle even without a haggling word:
    # "4000 per night ho jayega?" is the commonest opening in the transcripts.
    return offered_price(text) is not None


# ── the other intents ────────────────────────────────────────────────────────
#
# These are detected and recorded rather than forced. Recording them is what
# makes the evaluation report able to say "the guest asked to cancel and no tool
# ran", which is how the cancellation gap was found in the first place; forcing
# them needs either an id the conversation does not have or a decision a person
# should make. The two exceptions are `payment`/`booking_status`, which are
# answered by a read that takes no arguments at all.

_PAYMENT_LINK = re.compile(
    r"(?<![a-z0-9])(?:payment link|pay link|link bhej|link bheje|link send|"
    r"send (?:the |me )?link|payment karna|pay karna|pay kar|book kar do|"
    r"booking kar do|confirm kar do|advance kaise|kaise pay|how do i pay|"
    r"how to pay|upi|qr code)(?![a-z0-9])",
    re.IGNORECASE,
)
_PAYMENT_STATUS = re.compile(
    r"(?<![a-z0-9])(?:payment (?:ho ?gayi|hogyi|hogayi|hui|done|received|gaya|"
    r"gone through|successful)|paise (?:bhej|gaye|kat)|paid|"
    r"transaction (?:done|complete)|check karo|check kar do|"
    r"received the payment|payment mil (?:gaya|gayi)|paisa mila|"
    r"booking confirm|confirm hui|confirmed\?|status)(?![a-z0-9])",
    re.IGNORECASE,
)
_PAYMENT_STATUS_HI = re.compile(r"भुगतान हो|पैसे (?:भेज|कट)|पुष्टि")

_CANCELLATION = re.compile(
    r"(?<![a-z0-9])(?:cancel|cancell?ation|cancel karna|cancel karni|cancel kar do|"
    r"refund|refundable|paisa wapas|paise wapas|money back|"
    r"booking rad|rad kar)(?![a-z0-9])",
    re.IGNORECASE,
)
_CANCELLATION_HI = re.compile(r"रद्द|कैंसिल|वापस")

_MODIFICATION = re.compile(
    r"(?<![a-z0-9])(?:reschedule|postpone|prepone|change (?:the )?dates?|"
    r"dates? change|dates? badal|tarikh badal|shift (?:the )?booking|"
    r"modify|preponed|postponed|extend (?:the |my )?stay|ek din aur)(?![a-z0-9])",
    re.IGNORECASE,
)

# **A guest asking for a person, composed rather than listed.**
#
# The old pattern was an alternation of whole phrases, and it did not contain
# *"human se baat karni hai"* — which is the example the reliability brief
# itself gives, and one of the commonest ways an Indian guest asks. It held
# `kisi se baat` and `insaan se` and `manager se`, so the shape was known; the
# noun was simply missing from the list. §33 asks for 100% of explicit human
# requests recognised, and a hand-maintained list of phrases cannot get there:
# it has as many holes as there are nouns nobody happened to type.
#
# So the two halves are declared separately and multiplied. Adding a word a
# guest uses for a person now covers every construction at once, instead of one
# phrase at a time.
_PERSON = (
    r"human|insaan|insan|aadmi|admi|banda|bande|"
    r"manager|owner|malik|manejar|"
    r"staff|receptionist|reception|team|hotel wale|hotel walo|"
    r"person|somebody|someone|anybody|anyone|koi|kisi"
)

_ESCALATION_ASK = re.compile(
    r"(?<![a-z0-9])(?:"
    # Hinglish, which is postpositional: <person> se baat / <person> ko bulao.
    # The verb is deliberately open — `karni`, `karna`, `karwao`, `karao`,
    # `kara do` and a bare `baat` all mean the same request, and the gender
    # agreement that made `karni` a separate case in the guardrail is exactly
    # the kind of variation a phrase list keeps losing.
    rf"(?:{_PERSON})\s+(?:se|ko|si)\s+(?:baat|bat|baate|connect|milwa|milva|bula)|"
    rf"(?:{_PERSON})\s+(?:ko\s+)?(?:bula\s*(?:do|dijiye|o)|bulao|bulaiye)|"
    # English, which is prepositional.
    rf"(?:talk|speak|chat|connect)\s+(?:to|with)\s+(?:a\s+|an\s+|the\s+)?(?:{_PERSON})|"
    r"(?:connect|transfer|put)\s+me\s+(?:to|through|with|on)|"
    r"real person|actual person|live agent|human agent|"
    # A request for a call is a request for a person whoever makes it.
    r"call me|call kar|phone kar|phone me|ring me|callback|call back"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)

_QUOTE = re.compile(
    r"(?<![a-z0-9])(?:price|rate|rates|tariff|cost|charge|charges|"
    r"kitna|kitne|kitni|daam|keemat|how much|per night|total kitna|"
    r"quote|kya lagega|kya hoga)(?![a-z0-9])",
    re.IGNORECASE,
)
_QUOTE_HI = re.compile(r"कितना|कितने|दाम|कीमत|किराया")

_DATES = re.compile(
    r"(?<![a-z0-9])(?:\d{1,2}\s*(?:st|nd|rd|th)?\s*"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\w*|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\w*\s*\d{1,2}|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}|"
    r"tomorrow|tonight|today|weekend|next week|next month|"
    r"kal|parso|agle hafte|is weekend)(?![a-z0-9])",
    re.IGNORECASE,
)


# ── what a turn owes ─────────────────────────────────────────────────────────

def _stay_context(session: dict | None) -> dict:
    """
    The stay as structured state knows it. Never as the transcript describes it.

    Missing keys are missing on purpose — a caller that cannot find `check_in`
    here must not proceed, and a default would hide that.
    """
    session = session if isinstance(session, dict) else {}
    out: dict = {}
    for key in ("check_in", "check_out", "rate_plan_id", "room_type_id", "room_name"):
        value = session.get(key)
        if value:
            out[key] = str(value)
    for key in ("units", "adults", "children"):
        value = session.get(key)
        try:
            if value is not None:
                out[key] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def _valid_dates(stay: dict) -> bool:
    try:
        return date.fromisoformat(stay["check_out"]) > date.fromisoformat(stay["check_in"])
    except (KeyError, TypeError, ValueError):
        return False


def _group_requirement(message: str, session: dict | None) -> Requirement | None:
    rooms = room_count(message)
    people = people_count(message)
    worded = bool(_GROUP_WORDS.search(message or "") or _GROUP_WORDS_HI.search(message or ""))

    units = rooms
    source = "rooms"
    if units is None and people is not None and people >= GROUP_FROM_PEOPLE:
        units = max(1, -(-people // PEOPLE_PER_ROOM))  # ceil
        source = "people"

    enough = (
        (rooms is not None and rooms >= GROUP_FROM_ROOMS)
        or (units is not None and source == "people")
        or worded
    )
    if not enough:
        return None

    if units is None:
        # "group booking ke liye baat karni hai" with no number. The stay we
        # already know about is the best guess at scale, and failing that the
        # tool's own minimum is what the guest is asking about.
        units = _stay_context(session).get("units") or GROUP_FROM_ROOMS

    args: dict = {"units": int(units)}
    chosen = _stay_context(session).get("room_type_id")
    if chosen:
        # Their own room, so the bands come back for the thing they picked
        # rather than for the property in general — and so a room-count change
        # does not quietly lose the room they already chose. §9.
        args["room_type_id"] = chosen

    return Requirement(
        intent=GROUP_BOOKING,
        tool="get_group_rates",
        # A quote for the same number of rooms has already applied the band and
        # is a *better* answer than the bands themselves. See `Requirement`.
        satisfied_by=frozenset({"get_group_rates", "get_quote", "negotiate_rate"}),
        must_execute=True,
        arguments=args,
        detail=f"{units} room(s) from {source}" if rooms or people else "group wording",
        evidence={"rooms": rooms, "people": people, "worded": worded},
    )


def _negotiation_requirement(message: str, session: dict | None) -> Requirement | None:
    if not wants_discount(message):
        return None

    stay = _stay_context(session)
    asked = offered_price(message)
    evidence = {"requested_nightly_rupees": asked}

    if not (stay.get("rate_plan_id") and _valid_dates(stay)):
        # Nothing to negotiate *about* yet — and this is the ordinary first
        # turn, not an edge case. "Best price kya de sakte ho?" arrives before
        # anything has been priced far more often than it arrives after.
        #
        # **The requirement is real; only the tool is different.** It used to be
        # advisory, with `negotiate_rate` named as the tool it wanted, and both
        # halves of that were wrong. Advisory meant the deterministic layer had
        # no opinion at precisely the moment the model was most likely to form a
        # bad one — a handoff on a turn where the hotel had an answer. And
        # naming `negotiate_rate` on a session with no rate plan would send the
        # model at a tool that cannot run: there is nothing to discount yet.
        #
        # What the turn actually owes is to find out what is being haggled over.
        # A hotelier asked "what's your best price" says "for which nights?" —
        # that is the required action, and `get_stay_options` is the tool that
        # is it.
        satisfied_by = frozenset({
            "get_stay_options", "get_quote", "search_availability",
            "negotiate_rate", "get_negotiation_policy", "propose_rate_exception",
            "escalate_to_team",
        })
        if _valid_dates(stay):
            # Dates but no plan: the lookup is fully constructible, so this is
            # forceable and the application will run it rather than hoping.
            return Requirement(
                intent=NEGOTIATION,
                tool="get_stay_options",
                satisfied_by=satisfied_by,
                must_execute=True,
                arguments={
                    "check_in": stay["check_in"],
                    "check_out": stay["check_out"],
                    "units": room_count(message) or stay.get("units") or 1,
                    "adults": stay.get("adults") or 2,
                    "children": stay.get("children") or 0,
                },
                detail="guest asked for a better price before anything was priced",
                evidence=evidence,
            )
        # No dates either. Required, because the guest is owed an answer and a
        # handoff is not one — but not *forceable*, because there is no honest
        # set of arguments to invent. The right move is to ask which nights,
        # which is a sentence rather than a tool call, and `arguments=None`
        # is how this module says "the model's turn, not mine".
        return Requirement(
            intent=NEGOTIATION,
            tool="get_stay_options",
            satisfied_by=satisfied_by,
            must_execute=True,
            arguments=None,
            detail="guest asked for a better price with no dates on the session",
            evidence=evidence,
        )

    args = {
        "rate_plan_id": stay["rate_plan_id"],
        "check_in": stay["check_in"],
        "check_out": stay["check_out"],
        "units": stay.get("units") or 1,
        "adults": stay.get("adults") or 2,
        "children": stay.get("children") or 0,
    }
    # A room-count change in the same breath as the haggle — "5 kamre chahiye,
    # thoda kam karo" — is about five rooms, not about the one on the session.
    rooms = room_count(message)
    if rooms:
        args["units"] = rooms
    if asked is not None:
        args["requested_nightly_rupees"] = asked

    return Requirement(
        intent=NEGOTIATION,
        tool="negotiate_rate",
        satisfied_by=frozenset({"negotiate_rate", "propose_rate_exception"}),
        must_execute=True,
        arguments=args,
        detail=f"guest asked for ₹{asked:,}" if asked else "guest asked for a better price",
        evidence=evidence,
    )


def _quote_requirement(message: str, session: dict | None,
                       *, group_enquiry: bool = False) -> Requirement | None:
    """
    "Kitna lagega?" — and whether the turn can answer it deterministically.

    **The failure this exists for.** A guest wrote *"Actually 5 kamre chahiye,
    10 log aa rahe hain"*. `get_group_rates` was forced and ran, and it returns
    what a band *is* — `discountPct: 8`, `depositPct: 30` — and no prices,
    because pricing needs a room type it does not have. Nothing else ran. So the
    model was holding two percentages and a room count, and it invented a whole
    rate card: ₹1,65,600, ₹2,13,600, ₹50,400, ₹59,200, ₹89,600. The guardrail
    refused the reply and the guest got a person instead of a price.

    The tool's own instruction was already right — *"get_quote already applies
    this band, call it and quote what comes back"* — and the model did not, and
    an instruction the model can ignore is not a control. QUOTE_REQUIRED was
    detected on that turn and was advisory, so the deterministic layer had an
    opinion and no power.

    **Which tool, and why not always the quote.** `get_quote` takes a fifteen-
    minute hold on real inventory. Forcing it on every price question would take
    holds all day for guests who are still browsing, which is a worse failure
    than the one being fixed. So:

        room and rate plan chosen   →  get_quote      the guest has picked; a
                                                      firm price and a hold are
                                                      what they are asking for
        dates only                  →  get_stay_options  indicative totals for
                                                      every room, and no hold
        neither                     →  recorded only  there is nothing to price
                                                      and a guessed date is
                                                      worse than no call

    Both of the forceable branches return money with display strings beside it
    — see `money.with_display` — so what the model is handed is what it says.
    """
    asked_in_words = bool(_QUOTE.search(message or "") or _QUOTE_HI.search(message or ""))
    if not (asked_in_words or group_enquiry):
        return None

    stay = _stay_context(session)
    satisfied_by = frozenset({"get_quote", "get_stay_options", "negotiate_rate",
                              "get_group_rates", "search_availability"})

    if not _valid_dates(stay):
        # Advisory, as before. Recorded so the evaluation still reports "the
        # guest asked what it costs and nothing priced anything", which is how
        # this was found.
        return Requirement(
            intent=QUOTE_REQUIRED, tool="get_quote", satisfied_by=satisfied_by,
            must_execute=False, arguments=None,
            detail="guest asked what it costs, with no dates to price",
        )

    units = room_count(message) or stay.get("units") or 1
    people = people_count(message) or stay.get("adults") or 2
    common = {
        "check_in": stay["check_in"], "check_out": stay["check_out"],
        "units": int(units), "adults": int(people),
    }

    if stay.get("room_type_id") and stay.get("rate_plan_id"):
        return Requirement(
            intent=QUOTE_REQUIRED, tool="get_quote", satisfied_by=satisfied_by,
            must_execute=True,
            arguments={**common, "room_type_id": stay["room_type_id"],
                       "rate_plan_id": stay["rate_plan_id"]},
            detail="guest asked what it costs, and has chosen a room",
        )

    return Requirement(
        intent=QUOTE_REQUIRED, tool="get_stay_options", satisfied_by=satisfied_by,
        must_execute=True, arguments=common,
        detail="guest asked what it costs; pricing every room rather than "
               "holding one they have not chosen",
    )


def _status_requirement(message: str) -> Requirement | None:
    text = message or ""
    if not (_PAYMENT_STATUS.search(text) or _PAYMENT_STATUS_HI.search(text)):
        return None
    return Requirement(
        intent=BOOKING_STATUS,
        tool="get_booking_status",
        satisfied_by=frozenset({"get_booking_status", "get_reservation",
                                "list_guest_reservations"}),
        # Takes no arguments and reads only this conversation's own booking, so
        # there is nothing to get wrong and nothing to invent. This is the tool
        # whose absence produced "aapki booking tab confirm hogi jab payment
        # process hogi" twenty-six seconds after the money had landed.
        must_execute=True,
        arguments={},
        detail="asked whether the payment or booking went through",
    )


def _simple(intent: str, tool: str, satisfied_by: set[str], detail: str) -> Requirement:
    return Requirement(
        intent=intent, tool=tool, satisfied_by=frozenset(satisfied_by),
        must_execute=False, arguments=None, detail=detail,
    )


# Priority order. A message that is several things at once gets them in the
# order a hotel would deal with them: know the scale before pricing it, price
# it before haggling over it, and never let a haggle jump the queue in front of
# whether the guest can even be accommodated.
_PRIORITY = (
    ESCALATION, BOOKING_CANCELLATION, BOOKING_STATUS, GROUP_BOOKING,
    NEGOTIATION, PAYMENT, BOOKING_MODIFICATION, QUOTE_REQUIRED,
    ROOM_SELECTION, DATE_CHANGE, GUEST_COUNT_CHANGE,
)

# How many of one turn's requirements the application will run itself.
#
# Two, not all of them. A turn that forced four tools has stopped being a
# conversation and started being a batch job, and the latency the guest waits
# through is the same latency either way.
MAX_FORCED = 2


def stated_facts(message: str) -> dict:
    """
    What the guest said about their stay, in their own words, this turn.

    **The highest-priority source there is, and until now there was no path for
    it into the session at all.** Session facts were written from *tool
    arguments* — whatever the model happened to pass — so "Actually 4 log hain"
    updated the party only if some tool ran carrying it. Whether a correction
    stuck depended on which requirement happened to fire, which is not a
    property anybody designed.

    It matters because of what the session then feeds: `prompts` tells the model
    what the guest has settled on, and `session.DERIVED_FROM` decides what stops
    being true when they change it. Both need the correction to arrive, and a
    guest correcting themselves in prose — with no tool call on the turn — is
    the ordinary way it happens.

    Counts only, deliberately. Rooms and people are read straight out of the
    sentence by patterns that already exist and are already tested on 274
    phrasings. Dates need a parser and a timezone and would be guessing; a room
    name needs the property's own room list, which this module has no database
    to ask. Both arrive through tool arguments as they always have.
    """
    facts: dict = {}
    rooms = room_count(message)
    if rooms is not None:
        facts["units"] = rooms
    people = people_count(message)
    if people is not None:
        facts["adults"] = people
    # The door they are standing at, when they named one. `create_task` takes a
    # `room_number`, and a guest reporting a broken air conditioner has just
    # said which room — carrying it means the ticket goes to the right door
    # rather than to whichever one the model remembered.
    door = room_number(message)
    if door is not None:
        facts["room_number"] = door
    return facts


def detect(message: str, session: dict | None = None) -> list[Requirement]:
    """
    Everything this turn owes, most pressing first.

    Empty for most messages, which is the point: a greeting, a thank-you and a
    question about parking require no business action, and this must cost
    nothing on those turns.
    """
    text = (message or "").strip()
    if not text:
        return []

    found: list[Requirement] = []

    if _ESCALATION_ASK.search(text):
        # **Forced, and the only forced action whose effect is to stop.**
        #
        # It was advisory, which meant a guest asking for a human got one if and
        # only if the model noticed. That was the shape of every other
        # requirement here before this module existed, and it is least
        # defensible on this one: the other requirements are about a hotel
        # losing a sale, and this one is about a person asking for a person.
        #
        # It matters more now than it did. `services/ai/arbiter.py` turns down
        # handoffs the conversation does not support, so the system has started
        # having opinions about when the agent may stop — and a layer that can
        # say "not yet" needs a counterweight that can say "now". This is it,
        # and the two agree by construction: both read `_ESCALATION_ASK`, so a
        # sentence that forces a handoff can never be one the arbiter turns
        # down.
        #
        # Forcing a *stop* is also the one forced action that cannot overreach.
        # Every other one moves stock, money or a booking; this parks a thread
        # with a person, which is what the guest asked for and what a false
        # positive costs nothing worse than.
        found.append(Requirement(
            intent=ESCALATION,
            tool="request_human_handoff",
            satisfied_by=frozenset({"request_human_handoff", "escalate_to_team"}),
            must_execute=True,
            arguments={"reason": "guest_asked"},
            detail="guest asked for a person",
        ))

    if _CANCELLATION.search(text) or _CANCELLATION_HI.search(text):
        # Forced, and forced to the *reading* tool.
        #
        # `get_cancellation_quote` takes no arguments, reads only this
        # conversation's own booking and cancels nothing, so there is nothing
        # to get wrong in running it. `cancel_booking` is deliberately not what
        # gets forced: cancelling a stay because a guest typed the word "cancel"
        # is exactly the class of thing a deterministic layer must never do.
        # The guest hears the refund, says yes, and the model calls the second
        # tool — which is the sequence a hotel would want a human to follow too.
        found.append(Requirement(
            intent=BOOKING_CANCELLATION,
            tool="get_cancellation_quote",
            satisfied_by=frozenset({
                "get_cancellation_quote", "cancel_booking",
                "get_cancellation_terms", "get_booking_status",
                "escalate_to_team",
            }),
            must_execute=True,
            arguments={},
            detail="guest asked about cancelling or a refund",
        ))

    status = _status_requirement(text)
    if status:
        found.append(status)

    group = _group_requirement(text, session)
    if group:
        found.append(group)

    negotiation = _negotiation_requirement(text, session)
    if negotiation:
        found.append(negotiation)

    if _PAYMENT_LINK.search(text):
        found.append(_simple(PAYMENT, "create_booking_payment_link",
                             {"create_booking_payment_link", "create_payment_link",
                              "get_booking_status", "get_quote"},
                             "guest asked to pay"))

    if _MODIFICATION.search(text):
        found.append(_simple(BOOKING_MODIFICATION, "get_booking_status",
                             {"get_booking_status", "get_reservation",
                              "search_availability", "get_stay_options",
                              "escalate_to_team"},
                             "guest asked to change an existing booking"))

    # **A group enquiry owes a price, whether or not the guest used a price
    # word.** "5 kamre chahiye, 10 log aa rahe hain" contains no "kitna" and no
    # "rate", so it raised only GROUP_BOOKING — and `get_group_rates` returns
    # what a band *is*, not what a stay costs. The model was left holding a
    # discount percentage and a room count and invented five prices.
    #
    # Nobody asks a hotel for five rooms without wanting to know what five rooms
    # cost. Both requirements together are exactly `MAX_FORCED`, and when no room
    # has been chosen both are reads that take no hold.
    #
    # **Not when a negotiation is already owed.** "Thoda kam karo na, 4000 per
    # night mein ho jayega?" contains "per night", so it reads as a price
    # question as well as a haggle — and `negotiate_rate` answers both. It
    # returns `publishedTotalPaise` and `counterTotalPaise`, which now arrive
    # with display strings beside them, so the model already has real money to
    # quote. Forcing a quote on top would take a fifteen-minute hold on the same
    # rooms to produce a number the turn already has.
    negotiating = any(r.intent == NEGOTIATION for r in found)
    quote = (None if negotiating else
             _quote_requirement(text, session, group_enquiry=group is not None))
    if quote:
        found.append(quote)

    if _DATES.search(text):
        found.append(_simple(DATE_CHANGE, "search_availability",
                             {"search_availability", "get_stay_options", "get_quote",
                              "negotiate_rate", "get_group_rates"},
                             "guest named dates"))

    if people_count(text) is not None and not group:
        found.append(_simple(GUEST_COUNT_CHANGE, "get_stay_options",
                             {"get_stay_options", "search_availability", "get_quote",
                              "get_group_rates", "get_room_types"},
                             "guest named a party size"))

    order = {intent: i for i, intent in enumerate(_PRIORITY)}
    found.sort(key=lambda r: order.get(r.intent, 99))

    # One requirement per intent — the vocabularies overlap by design and a
    # duplicate would be forced twice.
    seen: set[str] = set()
    return [r for r in found if not (r.intent in seen or seen.add(r.intent))]


def unmet(requirements: list[Requirement], outcomes: list) -> list[Requirement]:
    """
    Which of these the turn did not discharge.

    Only successful calls count, exactly as in `obligations.satisfied_by`: a
    `negotiate_rate` that was refused for lack of permission has not answered
    the guest, and treating a refusal as a discharge is how a requirement
    disappears without being met.
    """
    ran = {
        getattr(o, "tool", "") for o in (outcomes or [])
        if getattr(o, "ok", False)
    }
    return [r for r in requirements if not (r.satisfied_by & ran)]


def summarise(requirements: list[Requirement], outcomes: list, forced: list[str]) -> dict:
    """
    The turn's requirement story, for `agent_runs` and for the evaluation report.

    One small object rather than four columns of booleans, because the questions
    asked of it in production are not known in advance — "how often does the
    model call negotiate_rate by itself" was not a question anybody had until
    the replay started scoring differently on identical input.
    """
    outstanding = unmet(requirements, outcomes)
    still = {r.intent for r in outstanding}
    return {
        "intents": [r.intent for r in requirements],
        "required": [r.tool for r in requirements if r.must_execute],
        "satisfied": [r.intent for r in requirements if r.intent not in still],
        # Split, because the two mean different things about the build. A
        # required action left undone is a defect; an advisory intent left
        # undone is usually the model choosing a better answer than the one
        # this module would have guessed at.
        "unmet": sorted(r.intent for r in outstanding if r.must_execute),
        "observed_unmet": sorted(r.intent for r in outstanding if not r.must_execute),
        "forced": forced,
    }


def status_of(summary: dict) -> str:
    """
    One word for `agent_runs.required_action_status`.

        none       nothing was required
        model      the model called everything that was required
        forced     the application ran something the model did not
        unmet      something was required and did not happen

    `forced` and `unmet` can both be true of one turn; `unmet` wins, because the
    turn still owes the guest something and that is the number worth watching.
    """
    if not summary.get("required"):
        return "none"
    if summary.get("unmet"):
        return "unmet"
    if summary.get("forced"):
        return "forced"
    return "model"
