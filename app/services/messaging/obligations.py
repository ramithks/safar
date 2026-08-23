"""
What the guest asked for, and which parts of it have actually been answered.

**The failure this exists for.** A guest writes one message containing six
requests:

    "10 Sep ko 2 rooms chahiye, breakfast included hai? airport pickup bhi
     chahiye aur best price batao."

The agent calls `search_availability`, quotes a price, and replies. It has
answered two of six. Nothing in the system notices, because nothing in the
system ever represented the other four — the model's own attention is the only
thing tracking them, and a model that has just done three tool calls and written
a paragraph has stopped attending to the second clause of the first sentence.

`GROUND_RULES` already says *"when a guest asks two things in one message,
answer both"*. That instruction has been there throughout and the failure
happened anyway, which is the evidence that a prompt is the wrong place for it.

**So obligations are structured state, opened from the message and closed by
tool results.** Two rules make it cheap enough to run on every turn:

  * *No model call.* The classifier already spends one cheap call deciding what
    a message is about; a second call to decide what it *contains* would double
    the fixed cost of every turn to solve a problem that a vocabulary solves.
    So the opening side is deterministic matching over Hindi, Hinglish and
    English.
  * *No history replay.* Obligations live in `conversations.guest_profile`
    beside the session and the language, are merged rather than rebuilt, and are
    summarised into one short prompt line. Re-deriving them from the transcript
    would mean sending the transcript, which is the thing being avoided.

**Deliberately conservative in one direction.** A missed obligation costs a
follow-up sentence in the prompt that nobody needed; a *false* obligation makes
the agent chase something the guest never asked about, which reads as not
listening. So the vocabulary requires reasonably specific words and every topic
has a tool — or a set of tools — that unambiguously discharges it. A topic
nothing can close would be an obligation that never goes away, which is worse
than not tracking it.
"""
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# The most obligations one message may open.
#
# A message matching eight topics is far more likely to be a long complaint or a
# paste than a guest with eight questions, and eight lines of "still unanswered"
# in a prompt is noise the model will skim. Six is more than the worst real
# message in the transcripts.
MAX_PER_MESSAGE = 6

# How many turns an obligation may stay open before it stops being something the
# guest is waiting for.
#
# A guest who asked about breakfast eight turns ago and has since booked a room
# is not still waiting for the breakfast answer — they moved on, and reminding
# the model about it at that point produces a non-sequitur. Chosen to be longer
# than a clarifying exchange (two or three turns) and shorter than a subject
# change.
MAX_AGE_TURNS = 6


@dataclass(frozen=True)
class Topic:
    """One thing a guest can ask about, and what settles it."""

    key: str
    # What the prompt calls it. The guest's words, not the schema's.
    label: str
    # Any of these tools returning ok closes it.
    closed_by: frozenset[str]
    patterns: tuple[re.Pattern, ...]


def _words(*alternatives: str) -> re.Pattern:
    """
    A pattern that matches any of these as whole words, case-insensitively.

    Whole words matter more than it looks. `\\bac\\b` matches "AC" and not
    "back"; without the boundary, half this vocabulary fires on prose that
    happens to contain the letters.
    """
    joined = "|".join(alternatives)
    return re.compile(rf"(?<![a-z0-9])(?:{joined})(?![a-z0-9])", re.IGNORECASE)


# The vocabulary.
#
# Hindi and Hinglish are first-class rather than an afterthought: every real
# conversation in the transcripts is one of the three, and a topic list that
# only fires on English would be a feature that works for the guests who need it
# least. Devanagari is included where guests actually write it.
TOPICS: tuple[Topic, ...] = (
    Topic(
        key="availability",
        label="whether the rooms are free",
        closed_by=frozenset({"search_availability", "get_stay_options", "get_quote",
                             "negotiate_rate", "create_booking_payment_link"}),
        patterns=(
            _words("available", "availability", "vacancy", "khali"),
            re.compile(r"उपलब्ध|खाली"),
            # The Hinglish forms, but only next to something that could be a
            # room. **`milega` on its own is not an availability question**:
            # "cancel karne pe paisa wapas milega?" is about a refund, and the
            # bare word fired on it — a false obligation that would have the
            # agent check availability for a guest asking to cancel. Anchoring
            # it to a room noun is the difference between a vocabulary and a
            # word list.
            re.compile(
                r"(?:room|rooms|kamra|kamre|suite|villa|dorm|bed)"
                r"[^.?!]{0,25}(?:milega|milegi|mil sakta|mil sakti|chahiye|hai kya)",
                re.IGNORECASE,
            ),
            re.compile(
                r"(?:milega|milegi|mil sakta|mil sakti|chahiye|hai kya)"
                r"[^.?!]{0,25}(?:room|rooms|kamra|kamre|suite|villa|dorm|bed)",
                re.IGNORECASE,
            ),
        ),
    ),
    Topic(
        key="price",
        label="the price",
        closed_by=frozenset({"get_quote", "get_stay_options", "negotiate_rate",
                             "get_group_rates", "create_booking_payment_link"}),
        patterns=(
            _words("price", "prices", "rate", "rates", "cost", "costs", "charge",
                   "charges", "tariff", "kitna", "kitne", "daam", "keemat",
                   "rupaye", "rupees"),
            re.compile(r"कितना|कितने|दाम|कीमत"),
            re.compile(r"[₹]\s*\d"),
        ),
    ),
    Topic(
        key="negotiation",
        label="a better price",
        closed_by=frozenset({"negotiate_rate", "propose_rate_exception",
                             "get_negotiation_policy", "escalate_to_team"}),
        patterns=(
            _words("discount", "best price", "best rate", "cheaper", "kam karo",
                   "kam kar", "kam ho", "thoda kam", "negotiate", "offer",
                   "last price", "final price", "sasta", "kam se kam"),
            re.compile(r"कम कर|छूट|सस्ता"),
        ),
    ),
    Topic(
        key="group",
        label="a group booking",
        closed_by=frozenset({"get_group_rates", "negotiate_rate", "get_quote",
                             "escalate_to_team"}),
        patterns=(
            _words("group", "groups", "bulk", "block booking", "wedding", "shaadi",
                   "baraat", "corporate", "conference", "team outing"),
            # "we are 12 people", "20 log", "8 rooms chahiye" — a number large
            # enough that it is a party rather than a couple.
            re.compile(r"(?<![a-z0-9])(?:1[0-9]|[2-9][0-9])\s*(?:people|pax|guests?|log|logo|rooms?|kamre)",
                       re.IGNORECASE),
        ),
    ),
    Topic(
        key="meals",
        label="meals or breakfast",
        closed_by=frozenset({"get_meal_info", "get_property_knowledge"}),
        patterns=(
            _words("breakfast", "lunch", "dinner", "meal", "meals", "food",
                   "nashta", "nashte", "khana", "bhojan", "veg", "non veg",
                   "vegetarian", "buffet", "restaurant", "kitchen"),
            re.compile(r"नाश्ता|खाना|भोजन"),
        ),
    ),
    Topic(
        key="photos",
        label="photos of the rooms",
        closed_by=frozenset({"send_room_photos", "get_hotel_page_link"}),
        patterns=(
            _words("photo", "photos", "pic", "pics", "picture", "pictures", "image",
                   "images", "tasveer", "photo bhejo", "dikhao", "dikha"),
            re.compile(r"तस्वीर|फोटो|दिखा"),
        ),
    ),
    Topic(
        key="services",
        label="a transfer or an extra",
        closed_by=frozenset({"list_services", "order_service", "get_experiences",
                             "escalate_to_team", "create_task"}),
        patterns=(
            _words("pickup", "pick up", "drop", "transfer", "cab", "taxi", "car",
                   "airport", "railway", "station", "laundry", "parking",
                   "bonfire", "spa", "gaadi", "lene aa", "chhodne"),
            re.compile(r"हवाई अड्डा|स्टेशन|गाड़ी"),
        ),
    ),
    Topic(
        key="early_checkin",
        label="arriving early or leaving late",
        closed_by=frozenset({"get_checkin_options", "get_property_policies",
                             "get_property_knowledge"}),
        patterns=(
            _words("early check", "early checkin", "late check", "late checkout",
                   "check in time", "checkin time", "check out time", "checkout time",
                   "jaldi aa", "jaldi pahunch", "der se"),
            # "10 baje check-in", "can I arrive at 8am"
            re.compile(r"\d{1,2}\s*(?:baje|am|pm|:\d{2})[^.]{0,20}check", re.IGNORECASE),
            re.compile(r"check[- ]?(?:in|out)[^.]{0,20}\d{1,2}\s*(?:baje|am|pm|:\d{2})",
                       re.IGNORECASE),
        ),
    ),
    Topic(
        key="cancellation",
        label="cancellation or a refund",
        closed_by=frozenset({"get_cancellation_terms", "get_property_policies",
                             "get_booking_status", "escalate_to_team"}),
        patterns=(
            _words("cancel", "cancellation", "refund", "refundable", "paisa wapas",
                   "paise wapas", "money back", "reschedule", "postpone", "change dates"),
            re.compile(r"रद्द|वापस"),
        ),
    ),
    Topic(
        key="amenities",
        label="what the room has",
        closed_by=frozenset({"get_room_details", "get_room_types",
                             "get_property_knowledge", "get_property"}),
        patterns=(
            _words("wifi", "wi-fi", "internet", "ac", "pool", "swimming", "gym",
                   "geyser", "hot water", "balcony", "view", "bed", "extra bed",
                   "amenities", "facilities", "suvidha"),
            re.compile(r"सुविधा|बिस्तर"),
        ),
    ),
    Topic(
        key="payment",
        label="the payment",
        closed_by=frozenset({"get_booking_status", "create_booking_payment_link",
                             "create_payment_link", "get_payment_terms"}),
        patterns=(
            _words("payment", "pay", "paid", "advance", "token", "deposit", "upi",
                   "card", "razorpay", "bhugtan", "paisa", "payment hogyi",
                   "payment ho gayi", "transaction"),
            re.compile(r"भुगतान|पैसे"),
        ),
    ),
    Topic(
        key="booking",
        label="making the booking",
        closed_by=frozenset({"create_booking", "create_booking_payment_link",
                             "get_booking_status", "get_quote"}),
        patterns=(
            _words("book", "booking", "reserve", "reservation", "confirm",
                   "book kar", "book karo", "kar do", "final kar"),
            re.compile(r"बुक|आरक्षण"),
        ),
    ),
    Topic(
        key="location",
        label="where the hotel is",
        closed_by=frozenset({"get_property", "get_property_knowledge",
                             "get_hotel_page_link"}),
        patterns=(
            _words("where", "location", "address", "how far", "distance", "directions",
                   "map", "kahan", "kaha", "kitni door", "pata"),
            re.compile(r"कहाँ|कहां|पता|दूर"),
        ),
    ),
)

_BY_KEY = {t.key: t for t in TOPICS}


def detect(message: str) -> list[str]:
    """
    Which topics this message raises. Ordered as `TOPICS` is, capped.

    **A question mark is not required and neither is a verb.** "airport pickup
    bhi chahiye" is a request, "breakfast included hai?" is a question, and
    "photos?" is one word — all three are things the guest expects an answer
    about, and only the vocabulary distinguishes them from prose that mentions
    the same noun.
    """
    text = (message or "").strip()
    if not text:
        return []

    found = [
        topic.key for topic in TOPICS
        if any(pattern.search(text) for pattern in topic.patterns)
    ]
    if len(found) > MAX_PER_MESSAGE:
        logger.debug("message raised %d topics; keeping %d", len(found), MAX_PER_MESSAGE)
    return found[:MAX_PER_MESSAGE]


def satisfied_by(outcomes: list) -> set[str]:
    """
    Which topics this turn's successful tool calls discharge.

    Only `ok` outcomes count. A `get_meal_info` that failed has not answered
    anything about breakfast, and treating a refusal as an answer is how an
    obligation disappears without being met.
    """
    tools = {
        getattr(o, "tool", "") for o in (outcomes or [])
        if getattr(o, "ok", False)
    }
    return {t.key for t in TOPICS if t.closed_by & tools}


def open_from(message: str, outcomes: list, previous: dict | None = None) -> dict:
    """
    The obligation map after this turn.

    Shaped `{topic: age_in_turns}`. An integer rather than a timestamp because
    what matters is how many exchanges have passed, not how long the guest took
    to reply — somebody who answers the next morning has not stopped waiting for
    the thing they asked last night.

    Ordering inside one turn: **raise first, then discharge.** A guest who asks
    about breakfast in the same message that triggers `get_meal_info` has had
    their question answered on that turn, and opening an obligation only to
    carry it forward would make the agent repeat itself.
    """
    previous = previous if isinstance(previous, dict) else {}

    aged: dict[str, int] = {}
    for key, age in previous.items():
        if key not in _BY_KEY:
            continue
        try:
            next_age = int(age) + 1
        except (TypeError, ValueError):
            continue
        if next_age <= MAX_AGE_TURNS:
            aged[key] = next_age

    for key in detect(message):
        # Re-asking resets the clock. A guest repeating a question is more
        # patient than one asking it once, not less, and letting the second ask
        # age out on the first ask's clock is how it goes unanswered twice.
        aged[key] = 0

    for key in satisfied_by(outcomes):
        aged.pop(key, None)

    return aged


def describe(profile: dict | None) -> str | None:
    """
    The open obligations as a prompt line, or None when there are none.

    None is the common case and costs nothing, which is the point: this line
    appears on the turns where the guest asked several things and is absent on
    the turns where they asked one.

    Deliberately phrased as a reminder rather than an instruction to answer
    everything now. Some of these need a tool the agent will call anyway; some
    are genuinely waiting on the hotel. Telling the model *what is outstanding*
    and letting it decide the order is the difference between a helpful nudge
    and a checklist it recites at the guest.
    """
    obligations = (profile or {}).get("obligations")
    if not isinstance(obligations, dict) or not obligations:
        return None

    labels = [
        _BY_KEY[key].label for key in sorted(obligations, key=lambda k: obligations[k])
        if key in _BY_KEY
    ]
    if not labels:
        return None

    listed = labels[0] if len(labels) == 1 else (
        ", ".join(labels[:-1]) + " and " + labels[-1]
    )
    return (
        f"This guest has asked about {listed} and has not been given an answer "
        "yet. Deal with whatever you can this turn; if something needs the "
        "hotel, say so rather than leaving it unmentioned."
    )
