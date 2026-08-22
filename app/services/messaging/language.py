"""
Which language a guest is spoken to in.

A guest wrote *"aur hinglish m baat karo please"* to a real hotel. Two turns
later the agent answered in Devanagari, and again an hour later, because
nothing anywhere remembered the request. The model is told "reply in the
language the guest wrote in" and does — for that message. A preference is not a
message; it survives a gap, a new thread and a change of subject, and only a
column can hold it.

**Three values, not a locale list.** `en`, `hi` and `hinglish` are what Indian
hotel guests actually use, and Hinglish is the one a locale table cannot
express: it is Hindi written in Latin script, and to Meta, to Postgres and to
every language-detection library it looks like broken English. It is also, on
this product's traffic, the most common of the three.

**Stored on the conversation, mirrored to the guest.** The conversation is
where a preference is expressed and where the next message will arrive, so it is
the authority. `guests.preferences` is the durable copy, and it only exists once
somebody is a guest rather than a stranger who said hello — see
`remember_preference`.

**The current message always wins.** This is a default for composing something
*we* send unprompted — a booking confirmation, a payment receipt — and a hint
for the model on a turn where the guest's own language is ambiguous. It never
overrides what somebody just typed. A guest who switches to English gets
English, and the stored value follows them rather than arguing.
"""
import logging
import re

from app.db.client import get_db

logger = logging.getLogger(__name__)

EN = "en"
HI = "hi"
HINGLISH = "hinglish"

SUPPORTED = (EN, HI, HINGLISH)
DEFAULT = EN

# Devanagari. If a message contains any of this it is Hindi, full stop — there
# is no Hinglish written in Devanagari, because Hinglish *is* the Latin one.
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

# Latin-script Hindi, by its function words rather than its content words.
#
# Deliberately closed and deliberately small. Content words drift between
# languages — "hotel", "book", "room" and "payment" are Hinglish and English
# alike — but the words that hold a Hindi sentence together do not appear in an
# English one at all. "Kya aap hindi m baat kar sakte h" contains four of these;
# "Do you have photos" contains none.
#
# Two hits, not one: "hai" alone is a name, and "me" is an English pronoun that
# happens to be a Hindi postposition. Two is enough to be a sentence and rare
# enough to be an accident.
_HINGLISH_MARKERS = frozenset({
    "hai", "hain", "h", "ha", "haan", "haa", "haa", "nahi", "nhi", "nahin",
    "kya", "kyu", "kyun", "kaise", "kaisa", "kitna", "kitne", "kab", "kahan",
    "aap", "aapka", "aapko", "aapke", "mera", "meri", "mere", "hum", "hume",
    "mujhe", "main", "mai", "m", "tum", "unka",
    "ka", "ki", "ke", "ko", "se", "mein", "par", "aur", "ya", "bhi", "to",
    "karo", "karna", "karke", "kar", "karta", "karte", "kardo", "kardi",
    "chahiye", "chahhiye", "chaiye", "hoga", "hogi", "hogya", "hogyi", "gaya",
    "gayi", "raha", "rahi", "rhe", "diya", "dena", "lena", "liye", "wala",
    "achha", "acha", "theek", "thik", "sahi", "bilkul", "abhi", "phir",
    "jaldi", "kripya", "please", "bhai", "sir", "madam", "din", "log", "logo",
})

_WORD = re.compile(r"[a-z]+")


def detect(message: str | None) -> str | None:
    """
    The language of one message, or None when there is nothing to go on.

    None is a real answer and the common one: "ok", "yes", a room number, an
    emoji. Returning a guess there is how a preference gets overwritten by a
    two-letter reply, which is the failure this module exists to prevent.
    """
    text = (message or "").strip()
    if not text:
        return None

    if _DEVANAGARI.search(text):
        return HI

    words = _WORD.findall(text.lower())
    if len(words) < 2:
        # One word carries no grammar. "Hi", "ok", "Sahil" — all three are
        # every language at once.
        return None

    hits = sum(1 for w in words if w in _HINGLISH_MARKERS)
    if hits >= 2:
        return HINGLISH
    return EN if len(words) >= 3 else None


def normalise(value: str | None) -> str | None:
    """A stored value we still recognise, or None."""
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in SUPPORTED else None


def of_conversation(conversation: dict | None) -> str:
    """
    What we should write in, for this thread. Falls back to English.

    Reads the row it was given rather than the database: every caller already
    has the conversation, and a query per outbound message to learn a fact that
    arrived with it is a query for nothing.
    """
    profile = (conversation or {}).get("guest_profile") or {}
    if not isinstance(profile, dict):
        return DEFAULT
    return normalise(profile.get("language")) or DEFAULT


def remember(conversation_id: str, language: str) -> None:
    """
    Store the preference on the thread. Best effort, never on the critical path.

    Merged rather than replaced: `guest_profile` also holds session state, and
    a language write that clobbered the quote a guest is mid-way through would
    be a far worse bug than the one this fixes.
    """
    normalised = normalise(language)
    if not normalised:
        return
    try:
        db = get_db()
        rows = (
            db.table("conversations").select("guest_profile")
            .eq("id", conversation_id).limit(1).execute().data or []
        )
        profile = (rows[0].get("guest_profile") if rows else None) or {}
        if not isinstance(profile, dict):
            profile = {}
        if profile.get("language") == normalised:
            return
        profile["language"] = normalised
        db.table("conversations").update({"guest_profile": profile}).eq(
            "id", conversation_id
        ).execute()
    except Exception:  # noqa: BLE001
        logger.warning("could not store language for conversation %s",
                       conversation_id, exc_info=True)


def instruction(language: str) -> str | None:
    """
    What to tell the model, when we know something it cannot see.

    Only for a *remembered* preference. The model reads the current message
    perfectly well; what it cannot read is a request made forty minutes and one
    subject change ago.
    """
    if language == HINGLISH:
        return (
            "This guest has asked to be spoken to in Hinglish — Hindi written in "
            "the Latin alphabet, the way they write themselves. Do not reply in "
            "Devanagari. If they switch to English or Hindi, follow them."
        )
    if language == HI:
        return (
            "This guest has asked to be spoken to in Hindi. If they switch "
            "language, follow them."
        )
    return None
