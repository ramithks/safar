"""
Ad copy a hotel can edit, built from facts a hotel actually gave us.

§28–§31 ask for one-click WhatsApp and Instagram ads with generated headlines,
primary text and a call to action, and for the hotel to be able to edit all of
it. The temptation is to hand a room and a marketing profile to a model and use
what comes back. That is wrong here for a reason worth stating:

**An ad is a public promise, and a hallucinated one is a promise the hotel has
to keep.** A model given "Lake View, ₹3,200, Udaipur" will write "the finest
lakeside views in Rajasthan, complimentary breakfast, free airport pickup" and
be right about none of it. `claims.py` already catches invented *numbers* on
campaign bodies, and numbers are the easier half — an invented amenity has no
digit in it.

So: **templates, filled from structured data, with a model nowhere in the
path.** Every phrase in every template is either fixed text or a slot, and every
slot is fed from a column somebody typed into their own dashboard. There is no
sentence this file can produce that the hotel did not supply the facts for.

That is a smaller feature than "AI writes your ads" and it is the one that can
be shipped. The hotelier edits every field afterwards — which is §28's actual
requirement, and which also makes the templates a starting point rather than a
claim. `ad_creatives.edited` records whether they did.

**Formats.** Single image, carousel (up to ten photos in order, each with its
own caption), video, and an Instagram post the hotel has already published. The
last one matters more than it looks: a reel that already performed is a better
ad than anything generated, and boosting it needs no new creative at all.
"""
import logging
from dataclasses import dataclass

from app.services.marketing import claims

logger = logging.getLogger(__name__)

# Meta's own limits, at the time of writing, on the fields a hotelier edits.
#
# Enforced here rather than discovered at publish time: a hotelier who writes a
# forty-word headline should be told while they are writing it, not after
# pressing Publish and waiting for an API round trip to refuse it.
MAX_HEADLINE = 40
MAX_PRIMARY = 125
MAX_CAROUSEL_CARDS = 10
MIN_CAROUSEL_CARDS = 2

# What a viewer is asked to do. Mapped to Meta's own CTA vocabulary at publish
# time; these are the words a hotelier chooses from.
CTAS: dict[str, str] = {
    "whatsapp": "Message us on WhatsApp",
    "instagram_dm": "Send us a DM",
    "website": "Check availability",
    "hotel_page": "See rooms and prices",
}


@dataclass(frozen=True)
class Creative:
    """One generated draft. Every field is editable afterwards."""

    template_key: str
    headline: str
    primary_text: str
    cta: str
    format: str
    media: list[dict]

    def as_dict(self) -> dict:
        return {
            "templateKey": self.template_key,
            "headline": self.headline,
            "primaryText": self.primary_text,
            "cta": self.cta,
            "format": self.format,
            "media": self.media,
        }


def _rupees(paise: int | None) -> str | None:
    if not paise:
        return None
    return f"₹{round(paise / 100):,}"


# Words an ad may not end on.
#
# Found by generating copy from a real hotel's real description and reading it:
# "A corner room on the upper floor with a private balcony over the…" — a
# truncation that is grammatically incomplete in a way a reader notices, in a
# public ad. Cutting at a word boundary is not enough; the *last* word has to be
# one a phrase can end on.
_DANGLING = frozenset({
    "a", "an", "the", "and", "or", "but", "with", "for", "from", "to", "of",
    "in", "on", "at", "by", "over", "under", "near", "into", "onto", "its",
    "our", "your", "their", "his", "her", "is", "are", "was", "were", "has",
    "have", "that", "this", "these", "those", "as", "than", "so", "plus",
})


def _undangle(text: str) -> str | None:
    """
    Drop trailing words no phrase should end on. None if nothing survives.

    **Applied to every path, not only the truncating one**, which is what the
    test caught: a hotelier whose own description ends "…with a view of the" —
    a truncated paste, which is exactly how that happens — was under the length
    limit and went into an ad untouched. Where the text was already fine, this
    changes nothing: "Quiet, by the sea." ends on "sea".
    """
    words = text.split(" ")
    while words and words[-1].strip(",;:-.").lower() in _DANGLING:
        words.pop()
    return " ".join(words).rstrip(",;:-") or None


def _first_sentence(text: str | None, limit: int) -> str | None:
    """
    One sentence of the hotel's own description, trimmed to fit.

    Cut at a sentence boundary where there is one under the limit, otherwise at
    a word boundary — and either way, never ending on a word that leaves the
    phrase hanging. An ad that says less is better than one that trails off
    mid-thought.
    """
    if not text:
        return None
    clean = " ".join(str(text).split())

    if len(clean) <= limit:
        trimmed = _undangle(clean)
        if trimmed is None:
            return None
        # A sentence that was already complete keeps its full stop; one this
        # function shortened gets one, so it does not read as a fragment.
        return trimmed if trimmed == clean else trimmed + "."

    for stop in (". ", "! ", "? "):
        head = clean.split(stop)[0]
        if len(head) <= limit:
            trimmed = _undangle(head)
            return (trimmed + ".") if trimmed else None

    trimmed = _undangle(" ".join(clean[:limit].split(" ")[:-1]))
    return (trimmed + "…") if trimmed else None


# ── the templates ────────────────────────────────────────────────────────────
#
# Each is a function over facts, returning (headline, primary text) or None when
# the facts it needs are missing. Returning None rather than filling a gap with
# a plausible phrase is the whole design: a template that cannot be honest does
# not run.

def _t_rate_led(f: dict) -> tuple[str, str] | None:
    """The price, the place, and what it includes. The workhorse."""
    price, city, room = f.get("price"), f.get("city"), f.get("room_name")
    if not (price and city):
        return None
    nights = f.get("meal_line")
    head = f"{room or 'Rooms'} in {city} from {price}"
    body = " ".join(filter(None, [
        f_desc if (f_desc := f.get("description_short")) else f"{room or 'A room'} in {city}.",
        nights,
        "Message us and we'll hold it.",
    ]))
    return head[:MAX_HEADLINE], body[:MAX_PRIMARY]


def _t_experience_led(f: dict) -> tuple[str, str] | None:
    """What there is to do, with the stay as the second line."""
    title, city = f.get("experience_title"), f.get("city")
    if not title:
        return None
    head = title if len(title) <= MAX_HEADLINE else f"{title[:MAX_HEADLINE - 1]}…"
    body = " ".join(filter(None, [
        f.get("experience_description") or (f"With us in {city}." if city else None),
        f"From {p}." if (p := f.get("experience_price")) else None,
        "Ask us about dates.",
    ]))
    return head, body[:MAX_PRIMARY]


def _t_offer_led(f: dict) -> tuple[str, str] | None:
    """
    An actual discount, which only runs when there is an authorised one.

    Gated on `offer_pct` being present in the *campaign's* structured offer, so
    the number in the copy is one somebody entered on a form rather than one
    this file invented. `claims.check` verifies that again downstream.
    """
    pct, city = f.get("offer_pct"), f.get("city")
    if not pct:
        return None
    head = f"{pct}% off in {city}" if city else f"{pct}% off your stay"
    body = " ".join(filter(None, [
        f.get("description_short"),
        f"{pct}% off when you book direct with us.",
        f"Until {u}." if (u := f.get("offer_until")) else None,
    ]))
    return head[:MAX_HEADLINE], body[:MAX_PRIMARY]


def _t_audience_led(f: dict) -> tuple[str, str] | None:
    """Who the hotel says it is for, in the hotel's own words."""
    best_for, city = f.get("best_for"), f.get("city")
    if not best_for:
        return None
    head = f"{best_for.capitalize()} in {city}" if city else best_for.capitalize()
    body = " ".join(filter(None, [
        f.get("usp") or f.get("description_short"),
        f"From {p} a night." if (p := f.get("price")) else None,
        "Message us to check dates.",
    ]))
    return head[:MAX_HEADLINE], body[:MAX_PRIMARY]


def _t_plain(f: dict) -> tuple[str, str] | None:
    """
    The one that always runs.

    A hotel with nothing but a name and a city still gets a draft, because the
    alternative is an empty box and an instruction to write an ad, which is the
    screen everybody closes.
    """
    city, name = f.get("city"), f.get("hotel_name") or "our hotel"
    head = f"Stay at {name}"[:MAX_HEADLINE]
    body = " ".join(filter(None, [
        f.get("description_short") or (f"{name}, in {city}." if city else f"{name}."),
        "Message us for dates and prices.",
    ]))
    return head, body[:MAX_PRIMARY]


TEMPLATES: dict[str, tuple[str, callable]] = {
    "offer_led": ("A discount you are running", _t_offer_led),
    "rate_led": ("Your rate and what it includes", _t_rate_led),
    "experience_led": ("Something to do here", _t_experience_led),
    "audience_led": ("Who you are for", _t_audience_led),
    "plain": ("Just the hotel", _t_plain),
}

# The order they are offered in. Most specific first: a hotel running a real
# discount should lead with it, and a hotel with nothing configured falls all
# the way through to `plain`.
TEMPLATE_ORDER = ("offer_led", "rate_led", "experience_led", "audience_led", "plain")


# ── facts ────────────────────────────────────────────────────────────────────


def facts_from(
    *,
    property_row: dict,
    marketing: dict | None = None,
    room: dict | None = None,
    rate_paise: int | None = None,
    meal_plan: str | None = None,
    experience: dict | None = None,
    offer: dict | None = None,
) -> dict:
    """
    Everything the templates may draw on, and nothing else.

    A flat dict rather than the rows themselves, so a template cannot reach for
    a column nobody meant it to have — the floor price, an internal note, a
    guest's name. What is not in here cannot appear in an ad.
    """
    marketing = marketing or {}
    offer = offer or {}
    best_for = (marketing.get("best_for") or [None])[0]

    meal_line = {
        "CP": "Breakfast included.",
        "MAP": "Breakfast and dinner included.",
        "AP": "All meals included.",
    }.get((meal_plan or "").upper())

    return {
        "hotel_name": property_row.get("name"),
        "city": property_row.get("city"),
        "description_short": _first_sentence(
            (room or {}).get("description") or marketing.get("usp"), 70,
        ),
        "usp": _first_sentence(marketing.get("usp"), 70),
        "best_for": best_for,
        "room_name": (room or {}).get("name"),
        "price": _rupees(rate_paise),
        "meal_line": meal_line,
        "experience_title": (experience or {}).get("title"),
        "experience_description": _first_sentence((experience or {}).get("description"), 70),
        "experience_price": _rupees((experience or {}).get("price_paise")),
        # Only from the campaign's own structured offer. Never derived from a
        # floor, a target, or the gap between two rates — those are internal
        # numbers and an ad is the last place they should surface.
        "offer_pct": offer.get("discountPct") or offer.get("discount_pct"),
        "offer_until": offer.get("validUntil") or offer.get("valid_until"),
    }


# ── media ────────────────────────────────────────────────────────────────────


def build_media(
    *,
    fmt: str,
    photos: list[str] | None = None,
    photo_meta: dict | None = None,
    video_url: str | None = None,
    instagram_media_id: str | None = None,
) -> list[dict]:
    """
    The pictures, in the shape a creative stores them.

    **A carousel keeps its order and its per-card captions**, because order is
    the story — the room, then the view, then the pool — and a carousel whose
    cards arrive shuffled is a worse ad than a single image. The captions come
    from `photo_meta`, which is the same map the room editor writes.

    An `existing_post` carries no media at all: the creative is Meta's, already
    published, and copying its URLs here would create a second version of an
    asset that already exists and can change under us.
    """
    photos = photos or []
    meta = photo_meta or {}

    if fmt == "existing_post":
        return []
    if fmt == "video":
        return [{"kind": "video", "url": video_url}] if video_url else []
    if fmt == "carousel":
        return [
            {
                "kind": "image",
                "url": url,
                "caption": (meta.get(url) or {}).get("caption"),
                "position": index,
            }
            for index, url in enumerate(photos[:MAX_CAROUSEL_CARDS])
        ]
    return [{"kind": "image", "url": photos[0]}] if photos else []


def format_problem(fmt: str, media: list[dict], instagram_media_id: str | None) -> str | None:
    """
    Why this creative cannot be published yet, in a sentence, or None.

    Checked before Meta is called rather than after. A carousel with one photo
    is refused by the API with an error nobody outside advertising can read;
    "a carousel needs at least two photos" is the same refusal in the language
    of the person who has to fix it.
    """
    if fmt == "existing_post":
        return None if instagram_media_id else "Pick which of your posts to boost."
    if fmt == "video":
        return None if media else "Add a video."
    if fmt == "carousel":
        if len(media) < MIN_CAROUSEL_CARDS:
            return f"A carousel needs at least {MIN_CAROUSEL_CARDS} photos."
        return None
    return None if media else "Add a photo."


# ── generating ───────────────────────────────────────────────────────────────


def generate(facts: dict, *, destination: str = "whatsapp") -> list[Creative]:
    """
    Every draft this hotel's own facts can honestly support, best first.

    Returns more than one on purpose: a hotelier choosing between three
    versions edits the one they picked, while a hotelier handed a single draft
    either accepts it or closes the screen.
    """
    cta = CTAS.get(destination, CTAS["whatsapp"])
    drafts: list[Creative] = []
    for key in TEMPLATE_ORDER:
        _label, builder = TEMPLATES[key]
        try:
            made = builder(facts)
        except Exception:  # noqa: BLE001
            # A template that raises is a bug in that template, not a reason the
            # hotelier cannot advertise. Skipped and logged.
            logger.warning("ad template %s raised", key, exc_info=True)
            continue
        if not made:
            continue
        headline, primary = made
        drafts.append(
            Creative(
                template_key=key,
                headline=headline.strip(),
                primary_text=" ".join(primary.split()).strip(),
                cta=cta,
                format="single_image",
                media=[],
            )
        )
    return drafts


def check_copy(headline: str, primary_text: str, offer: dict | None) -> list[str]:
    """
    Whether edited copy asserts a number nobody authorised.

    The same check campaign bodies already go through, applied to the two fields
    a hotelier may type into. It runs on *their* text as well as on ours, which
    is the point: the templates cannot invent a figure, and this is what stops a
    hotelier pasting "50% off" into an ad whose offer says 10.

    Returns the problems as sentences. Empty means nothing objectionable — not
    that the ad is good.
    """
    verdict = claims.check(f"{headline}\n{primary_text}", offer or {})
    return list(getattr(verdict, "problems", []) or [])
