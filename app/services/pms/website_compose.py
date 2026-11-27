"""
A hotel's website, composed from the hotel's own records.

    properties · room_types · rate_plans · services · experiences
    meal_options · stay_time_rules · marketing_profiles · partners
                              │
                              ▼
                        compose(facts)
                              │
                              ▼
              headline · tagline · about · highlights
              amenities · faq · seo · hero · sections

**One click, and no model in the path.** `services/marketing/creatives.py`
already argues this for ad copy and the argument is stronger here: a website is
a public promise a hotel has to keep, and it is the promise a guest reads *last*
before paying. A model handed "harry, bhiwani, 6 rooms" writes "our rooftop
infinity pool overlooks the Aravallis" and is wrong about all of it — and unlike
an invented price, an invented amenity has no digit in it for `claims.py` to
catch.

So every sentence this module can produce is either fixed text or a slot, and
every slot is fed from a column somebody typed into their own dashboard. There
is no output here the hotel did not supply the facts for. The rewrite endpoint
is where a model is allowed near this text, and it may only rephrase words that
already exist — see `website_rewrite.py`.

**It never overwrites what a hotelier wrote.** `compose` takes the existing
draft and fills the gaps in it; a headline somebody edited survives regenerating
the page around it. What it does replace is content it wrote itself last time,
which is what makes "add six photos, then generate again" do anything.

**A band with no facts behind it is switched off, not filled.** A hotel with no
experiences gets no "Things to do" heading over an empty strip. That is the
whole difference between a generated page and a template with a hotel's name
dropped into it.
"""
import logging
import re
from datetime import datetime, timezone

from app.db.client import get_db, run_db
from app.services.pms import policies as pms_policies
from app.services.pms import website as website_service

logger = logging.getLogger(__name__)


# ── what kind of place this is, in the page's own words ─────────────────────
#
# Keyed on `properties.property_type`, which is a closed column the hotelier
# chose from during onboarding. Every phrase here is true of every hotel of that
# type — none of them claims a view, a pool, or a standard of service.

_HEADLINE = {
    "hotel": "A comfortable stay in the heart of {city}",
    "resort": "Room to slow down, in {city}",
    "homestay": "A home to come back to in {city}",
    "hostel": "Meet {city} from the middle of it",
    "boutique": "A small hotel with a point of view in {city}",
    "guesthouse": "A quiet room in {city}",
    "villa": "The whole place to yourselves in {city}",
}

_HEADLINE_NO_CITY = {
    "hotel": "A comfortable stay at {name}",
    "resort": "Room to slow down, at {name}",
    "homestay": "A home to come back to",
    "hostel": "A bed, and people to meet",
    "boutique": "A small hotel with a point of view",
    "guesthouse": "A quiet room at {name}",
    "villa": "The whole place to yourselves",
}

# The adjective in the tagline. Same rule: true of the category, never of the
# individual hotel.
_TONE = {
    "hotel": "straightforward",
    "resort": "unhurried",
    "homestay": "warm",
    "hostel": "sociable",
    "boutique": "considered",
    "guesthouse": "quiet",
    "villa": "private",
}

# Which look suits which kind of place, used only when the hotel has never
# chosen one. A suggestion, overwritten the moment an owner picks a template.
_TEMPLATE_FOR = {
    "hotel": "business",
    "resort": "resort",
    "homestay": "homestay",
    "hostel": "hostel",
    "boutique": "boutique",
    "guesthouse": "minimal",
    "villa": "luxury",
}

_ACCENT_FOR = {
    "boutique": "#7C3AED", "resort": "#0891B2", "luxury": "#A16207",
    "homestay": "#16A34A", "hostel": "#EA580C", "business": "#334155",
    "beach": "#0F766E", "mountain": "#1D4ED8", "minimal": "#0F172A",
    "heritage": "#9A3412",
}


# ── recognising an amenity a hotel typed in free text ───────────────────────
#
# Room amenities are a text array a hotelier fills in themselves, so "Wifi",
# "Wi-Fi", "wifi (free)" and "Free WiFi" are all in the data. Matched on a
# normalised string so the highlight strip and the FAQ agree with each other,
# and so neither claims something nobody wrote.

_AMENITY_PATTERNS: list[tuple[str, str, str]] = [
    # (highlight icon, regex over the normalised amenity, the words on the page)
    ("wifi", r"wi ?fi|wireless|internet", "Free Wi-Fi"),
    ("parking", r"parking|garage|valet", "Parking"),
    ("ac", r"\bac\b|air ?con", "Air conditioning"),
    ("pool", r"pool|swimming", "Swimming pool"),
    ("restaurant", r"restaurant|dining|cafe|kitchen", "Restaurant"),
    ("breakfast", r"breakfast", "Breakfast"),
    ("reception", r"24 ?7|24 ?hour|round the clock|reception", "24/7 reception"),
    ("pets", r"pet", "Pet friendly"),
    ("view", r"view|balcony|terrace", "Rooms with a view"),
    ("family", r"family|child|kid", "Family rooms"),
]


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (value or "").lower()).strip()


def _title(value: str) -> str:
    """A hotelier's own word, tidied but not rewritten."""
    text = (value or "").strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]


def _clock(value) -> str:
    """'14:00:00' → '2 pm'. What a guest reads, not what a column holds."""
    text = str(value or "")[:5]
    if not re.fullmatch(r"\d{2}:\d{2}", text):
        return ""
    hour, minute = (int(x) for x in text.split(":"))
    suffix = "am" if hour < 12 else "pm"
    twelve = hour % 12 or 12
    return f"{twelve}:{minute:02d} {suffix}" if minute else f"{twelve} {suffix}"


def _sentence(*parts: str) -> str:
    """Join the clauses that had facts behind them, and drop the ones that did not."""
    return " ".join(p.strip() for p in parts if p and p.strip())


def _rupees(paise) -> str:
    try:
        value = int(paise)
    except (TypeError, ValueError):
        return ""
    return f"₹{value // 100:,}"


# ── reading the hotel ───────────────────────────────────────────────────────

def _read_facts(property_id: str) -> dict:
    """
    Everything a page about this hotel could be built from, in one pass.

    Nine reads rather than nine round trips from the composer, because every one
    of them is needed for every generate and a partially-read hotel produces a
    page missing a band for no reason a hotelier could see.
    """
    db = get_db()

    prop = (
        db.table("properties")
        .select("id,partner_id,slug,name,property_type,address,city,state,country,"
                "pincode,lat,lon,check_in_time,check_out_time,total_rooms,policies")
        .eq("id", property_id).limit(1).execute().data or []
    )
    if not prop:
        return {}
    prop = prop[0]

    rooms = (
        db.table("room_types")
        .select("id,name,description,amenities,photos,max_occupancy,bed_type,"
                "size_sqft,view,children_allowed,total_units,accessible")
        .eq("property_id", property_id).eq("is_active", True)
        .order("sort_order").execute().data or []
    )
    plans = (
        db.table("rate_plans")
        .select("room_type_id,base_price_paise,meal_plan,refundable,"
                "cancellation_cutoff_hours,cancellation_policy")
        .eq("property_id", property_id).eq("is_active", True)
        .eq("audience", "public").execute().data or []
    )
    services = (
        db.table("services").select("name,description,category,price_paise")
        .eq("property_id", property_id).eq("is_active", True)
        .order("sort_order").limit(50).execute().data or []
    )
    experiences = (
        db.table("experiences").select("title,description,category,price_paise,photos")
        .eq("property_id", property_id).eq("is_active", True)
        .neq("availability", "not_available")
        .order("sort_order").limit(20).execute().data or []
    )
    meals = (
        db.table("meal_options").select("*")
        .eq("property_id", property_id).limit(10).execute().data or []
    )
    times = (
        db.table("stay_time_rules").select("*")
        .eq("property_id", property_id).eq("is_active", True).limit(50).execute().data or []
    )
    marketing = (
        db.table("marketing_profiles").select("*")
        .eq("property_id", property_id).limit(1).execute().data or []
    )
    partner = (
        db.table("partners").select("phone,whatsapp,email,instagram,about,brand_name")
        .eq("id", prop["partner_id"]).limit(1).execute().data or []
    )
    site = website_service._read(property_id) or {}

    return {
        "property": prop,
        "rooms": rooms,
        "plans": plans,
        "services": services,
        "experiences": experiences,
        "meals": meals,
        "times": times,
        "marketing": marketing[0] if marketing else {},
        "partner": partner[0] if partner else {},
        "draft": site.get("draft") or {},
    }


async def facts(property_id: str) -> dict:
    return await run_db(_read_facts, property_id)


# ── what we found ───────────────────────────────────────────────────────────

def inventory(found: dict) -> dict:
    """
    The "we already know this about your hotel" screen, as counts.

    Deliberately counts of rows rather than a readiness score. A hotelier about
    to press a button that rewrites their home page is asking "what is this
    going to be built from" — and eight room types and forty-two photographs is
    an answer to that, where "83% complete" is an answer to a different
    question.
    """
    prop = found.get("property") or {}
    rooms = found.get("rooms") or []
    photos = {p for r in rooms for p in (r.get("photos") or [])}
    gallery = [g for g in (found.get("draft") or {}).get("gallery") or [] if g]
    amenities = _amenities(found)
    plans = found.get("plans") or []
    meals = [m for m in found.get("meals") or [] if m.get("availability") not in (None, "not_available")]

    return {
        "hotelName": prop.get("name") or "",
        "city": prop.get("city"),
        "roomTypes": len(rooms),
        "rooms": sum(int(r.get("total_units") or 0) for r in rooms),
        "photos": len(photos) + len(gallery),
        "roomPhotos": len(photos),
        "galleryPhotos": len(gallery),
        "amenities": len(amenities),
        "rates": len(plans),
        "fromPricePaise": _from_price(plans),
        "services": len(found.get("services") or []),
        "experiences": len(found.get("experiences") or []),
        "meals": len(meals),
        "hasAddress": bool(prop.get("address")),
        "hasMap": prop.get("lat") is not None and prop.get("lon") is not None,
        "hasContact": bool((found.get("partner") or {}).get("phone")
                           or (found.get("partner") or {}).get("email")),
        "hasStory": bool((found.get("marketing") or {}).get("usp")),
        # Not a blocker — the composer builds a page without any of these — but
        # the two that visibly thin the result, so the screen can say so before
        # rather than after.
        "thin": [
            label for label, ok in (
                ("photographs", len(photos) + len(gallery) >= 3),
                ("room descriptions", any((r.get("description") or "").strip() for r in rooms)),
                ("published rates", bool(plans)),
            ) if not ok
        ],
    }


def _from_price(plans: list[dict]) -> int | None:
    prices = [p["base_price_paise"] for p in plans if p.get("base_price_paise")]
    return min(prices) if prices else None


# ── the pieces ──────────────────────────────────────────────────────────────

def _amenity_key(value: str) -> str:
    """
    The identity of an amenity, whatever a hotelier called it.

    **Normalising the string is not enough.** Two rooms typed "Free WiFi" and
    "wifi", and a third typed "AC" where the second said "Air conditioning" —
    all real, all in the same hotel — and a key of the tidied string made those
    six separate amenities. The grid then listed Wi-Fi twice with different
    capitalisation, which reads as a page built by a machine that has never
    looked at itself.
    """
    text = _norm(value)
    for _, pattern, _label in _AMENITY_PATTERNS:
        if re.search(pattern, text):
            return pattern
    return text


def _amenities(found: dict) -> list[str]:
    """
    What this hotel has, from what this hotel typed.

    Room amenities and facility-ish services, folded onto one entry per real
    thing. **The hotelier's own words win**: the first spelling encountered is
    the one shown, because "Free Wi-Fi" is what they chose to promise and our
    canonical label is only ever used to recognise it, never to replace it.
    Nothing is added that no room and no service mentions.
    """
    seen: dict[str, str] = {}

    def offer(raw: str) -> None:
        text = str(raw).strip()
        if not text:
            return
        key = _amenity_key(text)
        if key and key not in seen:
            seen[key] = _title(text)

    for room in found.get("rooms") or []:
        for raw in room.get("amenities") or []:
            offer(raw)
    for service in found.get("services") or []:
        if service.get("category") in ("facility", "wellness", "recreation"):
            offer(service.get("name") or "")
    return list(seen.values())[:24]


def _matched(found: dict) -> dict[str, str]:
    """Which known amenities this hotel actually has, icon → the words for it."""
    haystack = " ".join(_norm(a) for a in _amenities(found))
    hits: dict[str, str] = {}
    for icon, pattern, label in _AMENITY_PATTERNS:
        if re.search(pattern, haystack):
            hits[icon] = label
    return hits


def _breakfast(found: dict) -> dict | None:
    for meal in found.get("meals") or []:
        if meal.get("meal") == "breakfast" and meal.get("availability") not in (
            None, "not_available",
        ):
            return meal
    return None


def _highlights(found: dict) -> list[dict]:
    """
    The strip under the hero. Five at most, and every one of them checkable.

    "Best rate guaranteed" is not here. Nothing in this system guarantees a
    rate against an OTA, so it would be the one sentence on the page the hotel
    could not honour. "Book direct with the hotel" says the true half.
    """
    hits = _matched(found)
    out: list[dict] = [{"icon": "rate", "label": "Book direct with the hotel"}]

    breakfast = _breakfast(found)
    if breakfast:
        included = breakfast.get("availability") == "included"
        out.append({
            "icon": "breakfast",
            "label": "Breakfast included" if included else "Breakfast available",
        })

    for icon in ("wifi", "parking", "ac", "pool", "restaurant", "reception"):
        if icon in hits:
            out.append({"icon": icon, "label": hits[icon]})

    plans = found.get("plans") or []
    if plans and any(p.get("refundable") for p in plans):
        out.append({"icon": "cancellation", "label": "Free cancellation available"})

    return out[:5]


def _about(found: dict) -> str:
    """
    The hotel's paragraph, clause by clause, each one dropped when its fact is
    missing.

    Reads as prose because the clauses are ordered the way somebody describing
    their own hotel orders them — what it is, how big, why people come, what
    happens in the morning, when you can arrive — and not because a model
    smoothed it.
    """
    prop = found["property"]
    name = prop.get("name") or "This hotel"
    city = (prop.get("city") or "").strip()
    kind = prop.get("property_type") or "hotel"
    rooms = found.get("rooms") or []
    marketing = found.get("marketing") or {}

    where = f"in {_title(city)}" if city else ""
    first = _sentence(f"{name} is a {kind}", where).strip() + "."

    units = sum(int(r.get("total_units") or 0) for r in rooms)
    if units and len(rooms) > 1:
        second = f"There are {units} rooms across {len(rooms)} room types."
    elif units:
        second = f"There are {units} rooms."
    elif rooms:
        second = f"There {'is' if len(rooms) == 1 else 'are'} {len(rooms)} room type{'' if len(rooms) == 1 else 's'}."
    else:
        second = ""

    usp = (marketing.get("usp") or "").strip()
    third = usp if usp.endswith((".", "!", "?")) else (f"{usp}." if usp else "")

    best_for = [str(b).strip() for b in (marketing.get("best_for") or []) if str(b).strip()]
    fourth = ""
    if best_for:
        listed = best_for[0] if len(best_for) == 1 else (
            f"{', '.join(best_for[:-1])} and {best_for[-1]}"
        )
        fourth = f"It suits {listed}."

    breakfast = _breakfast(found)
    fifth = ""
    if breakfast:
        starts = _clock(breakfast.get("starts_at"))
        if breakfast.get("availability") == "included":
            fifth = f"Breakfast is included{f', served from {starts}' if starts else ''}."
        else:
            fifth = f"Breakfast is available{f' from {starts}' if starts else ''}."

    check_in = _clock(prop.get("check_in_time"))
    check_out = _clock(prop.get("check_out_time"))
    sixth = (
        f"Check-in is from {check_in} and check-out by {check_out}."
        if check_in and check_out else ""
    )

    # The reason this page exists at all, and the one claim it is entitled to
    # make about itself: booking here is booking with the hotel.
    last = "Booking on this page books you directly with the hotel."

    return " ".join(p for p in (first, second, third, fourth, fifth, sixth, last) if p)


def _faq(found: dict) -> list[dict]:
    """
    The questions guests ask, answered from the rows the AI employee reads.

    Every answer is resolved from the same tables `services/pms/policies.py`
    resolves for the agent, so a guest reading this page and a guest asking the
    same question on WhatsApp cannot be told two different things — which is
    the failure mode that makes an FAQ worse than no FAQ.
    """
    prop = found["property"]
    hits = _matched(found)
    out: list[dict] = []

    check_in = _clock(prop.get("check_in_time"))
    check_out = _clock(prop.get("check_out_time"))
    bands = [
        b for b in pms_policies.bands_from_rows(found.get("times") or [])
        if b.charge_kind != "unavailable"
    ]
    early = [b for b in bands if b.kind == "early_in"]
    late = [b for b in bands if b.kind == "late_out"]

    if check_in:
        answer = f"Check-in is from {check_in}."
        if early:
            answer += (
                f" Earlier arrivals can be arranged from {early[0].until.strftime('%H:%M')}"
                " — ask us when you book."
            )
        out.append({"q": "What time is check-in?", "a": answer})
    if check_out:
        answer = f"Check-out is by {check_out}."
        if late:
            answer += (
                f" A later departure until {late[-1].until.strftime('%H:%M')} may be possible,"
                " subject to availability."
            )
        out.append({"q": "What time is check-out?", "a": answer})

    breakfast = _breakfast(found)
    if breakfast:
        starts, ends = _clock(breakfast.get("starts_at")), _clock(breakfast.get("ends_at"))
        window = f" It is served{f' from {starts}' if starts else ''}{f' to {ends}' if ends else ''}." \
            if (starts or ends) else ""
        included = breakfast.get("availability") == "included"
        price = _rupees(breakfast.get("price_paise"))
        out.append({
            "q": "Is breakfast available?",
            "a": (
                f"Yes — breakfast is included in the room rate.{window}" if included
                else f"Yes.{f' It is {price} per person.' if price else ''}{window}"
            ).strip(),
        })

    if "parking" in hits:
        out.append({"q": "Is parking available?", "a": "Yes, parking is available at the property."})
    if "wifi" in hits:
        out.append({"q": "Is there Wi-Fi?", "a": "Yes — Wi-Fi is available throughout the property."})

    plans = found.get("plans") or []
    cheapest = min(
        (p for p in plans if p.get("base_price_paise")),
        key=lambda p: p["base_price_paise"], default=None,
    )
    if cheapest:
        # Resolved through the same function that decides what a guest is
        # actually refunded, rather than restated from the two columns. A page
        # that says "free cancellation" and a refund calculation that disagrees
        # is a chargeback.
        terms = pms_policies.cancellation_terms(
            cheapest.get("cancellation_policy"),
            refundable=bool(cheapest.get("refundable", True)),
            cutoff_hours=cheapest.get("cancellation_cutoff_hours"),
        )
        if terms.non_refundable:
            answer = "This rate is non-refundable — once booked, the amount paid is not returned."
        elif terms.free_until_hours is not None:
            hours = terms.free_until_hours
            when = (
                f"{hours // 24} day{'s' if hours // 24 != 1 else ''}"
                if hours >= 24 else f"{hours} hour{'s' if hours != 1 else ''}"
            )
            answer = f"Cancel at least {when} before check-in and you are refunded in full."
        else:
            answer = "Cancellation terms depend on the rate you book — they are shown before you pay."
        if terms.note:
            answer += f" {terms.note}"
        out.append({"q": "What is your cancellation policy?", "a": answer})

    rooms = found.get("rooms") or []
    if any((r.get("children_allowed") or "") not in ("", "no", None) for r in rooms):
        out.append({
            "q": "Can I bring children?",
            "a": "Yes — children are welcome. Tell us their ages when you book and we'll "
                 "make sure the room is set up for them.",
        })
    if "pets" in hits:
        out.append({"q": "Are pets allowed?", "a": "Yes, pets are welcome at this property."})

    address = (prop.get("address") or "").strip()
    if address:
        out.append({
            "q": "Where exactly are you?",
            "a": _sentence(address, _title(prop.get("city") or ""), prop.get("pincode") or ""),
        })

    return out[:8]


def _fit(text: str, limit: int) -> str:
    """
    Cut to a length a search result actually shows, at a sentence if possible.

    Never mid-word. A meta description ending "…served from 7:30 am. Check-" is
    what a naive slice produces, and it is the hotel's own listing on Google
    looking like a truncated database field.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    window = text[: limit + 1]
    # A full stop late enough to still be a description, not a fragment.
    stop = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if stop >= limit * 0.6:
        return window[: stop + 1].strip()
    space = window.rfind(" ")
    return (window[:space] if space > 0 else window[:limit]).rstrip(" ,;:-") + "…"


def _seo(found: dict, headline: str, about: str) -> dict:
    prop = found["property"]
    name = prop.get("name") or ""
    city = _title(prop.get("city") or "")
    title = f"{name}, {city} — book direct" if city else f"{name} — book direct"
    # The last sentence of `about` is ours, not the hotel's — it says booking
    # here books direct, which is true of every StaySuite page and therefore says
    # nothing about *this* hotel in a search result.
    description = about.split(" Booking on this page")[0].strip() or headline
    return {"title": _fit(title, 70), "description": _fit(description, 180)}


# The order photographs want to be seen in. A hotel's website opens on the
# building, not on a bathroom — and a gallery that opens on a bathroom is the
# single most common thing wrong with a hotel page built from an upload folder.
_PHOTO_RANK = {
    "exterior": 0, "pool": 1, "lobby": 2, "room": 3, "dining": 4,
    "amenity": 5, "surroundings": 6, "bathroom": 8, "other": 7,
}


def _gallery(found: dict) -> tuple[list[dict], str | None, bool]:
    """
    The hotel's photographs, ordered, and the one that goes at the top.

    Room photographs are *not* copied in here. They belong to the room and the
    rooms band already draws them; copying them would mean a hotel that
    replaces a room photo has an old one still sitting in its gallery. What the
    hero may fall back to is a room photo — a page with a picture on it beats a
    page with a coloured rectangle — but it is read live, not copied.
    """
    draft = found.get("draft") or {}
    existing = [
        item for item in (website_service.gallery_item(v) for v in (draft.get("gallery") or []))
        if item
    ]
    ordered = sorted(existing, key=lambda p: _PHOTO_RANK.get(p.get("category") or "other", 7))

    stored = (draft.get("heroImageUrl") or "").strip() or None
    # A hero naming a photograph that is no longer in the gallery.
    #
    # Reported rather than silently repaired, because whether it may be
    # replaced is not this function's call — except that it is: a stored hero
    # pointing at a deleted file renders as a broken image at the top of the
    # hotel's website, and that is broken whoever set it. `compose` uses this
    # flag to overwrite even a hero the hotelier chose, which is the one case
    # where replacing their choice is the respectful thing to do.
    dangling = bool(stored and stored not in {p["url"] for p in ordered})

    hero = None if dangling else stored
    if not hero and ordered:
        hero = ordered[0]["url"]
    if not hero:
        # A room's photograph, read live rather than copied — a page with a
        # picture on it beats a page with a coloured rectangle.
        for room in found.get("rooms") or []:
            if room.get("photos"):
                hero = room["photos"][0]
                break
    return ordered, hero, dangling


def _tagline(found: dict) -> str:
    prop = found["property"]
    kind = prop.get("property_type") or "hotel"
    city = _title(prop.get("city") or "")
    tone = _TONE.get(kind, "comfortable")
    hits = _matched(found)
    named = [hits[i] for i in ("wifi", "breakfast", "parking", "pool") if i in hits][:2]

    base = f"A {tone} stay in {city}" if city else f"A {tone} place to stay"
    if named:
        return f"{base}, with {' and '.join(n.lower() for n in named)}."
    return f"{base}."


def _headline(found: dict) -> str:
    prop = found["property"]
    kind = prop.get("property_type") or "hotel"
    city = _title(prop.get("city") or "")
    if city:
        return _HEADLINE.get(kind, _HEADLINE["hotel"]).format(city=city)
    return _HEADLINE_NO_CITY.get(kind, _HEADLINE_NO_CITY["hotel"]).format(
        name=prop.get("name") or "us",
    )


# Which fields `compose` wrote itself last time, and may therefore write again.
#
# The mechanism that makes "generate" safe to press twice: anything a hotelier
# has touched since is not in this set for their row, because the moment they
# save an edit the builder drops the field's name from it.
_AUTHORED_KEY = "generatedFields"


def compose(found: dict, *, force: bool = False) -> dict:
    """
    A complete draft for this hotel, layered over whatever is already there.

    `force` rewrites even the fields a hotelier has edited — the "start again"
    the builder offers behind a confirmation, and never the default.
    """
    draft = dict(found.get("draft") or {})
    prop = found["property"]
    partner = found.get("partner") or {}
    authored = set(draft.get(_AUTHORED_KEY) or []) if not force else None

    def writable(key: str) -> bool:
        """May this generate replace what is in `key`?"""
        current = draft.get(key)
        empty = current in (None, "", [], {})
        if empty:
            return True
        if force:
            return True
        # Non-empty: only if we are the ones who put it there.
        return authored is not None and key in authored

    written: list[str] = []

    def put(key: str, value) -> None:
        """
        Write a composed value, if this field is still ours to write.

        **An empty value clears a field we own.** A hotel that removes its
        breakfast row should not keep an FAQ entry answering "yes, breakfast is
        included" — that answer was composed from a fact that no longer exists,
        and leaving it there is the page contradicting the hotel's own records
        and its AI employee. A field the hotelier has edited is never ours, so
        nothing they wrote is cleared this way.
        """
        empty = value in (None, "", [], {})
        if not writable(key):
            return
        if empty:
            # Only clear what is actually there, and do not claim authorship of
            # a field this generate did not fill.
            if key in draft:
                draft[key] = value if isinstance(value, (list, dict)) else ""
            return
        draft[key] = value
        written.append(key)

    headline = _headline(found)
    about = _about(found)

    put("headline", headline)
    put("tagline", _tagline(found))
    put("about", about)
    put("highlights", _highlights(found))
    put("amenities", _amenities(found))
    put("faq", _faq(found))
    put("seo", _seo(found, headline, about))
    put("contactPhone", partner.get("phone") or partner.get("whatsapp"))
    put("contactEmail", partner.get("email"))

    gallery, hero, dangling = _gallery(found)
    if gallery:
        draft["gallery"] = gallery
    if hero and (writable("heroImageUrl") or not draft.get("heroImageUrl") or dangling):
        draft["heroImageUrl"] = hero
        written.append("heroImageUrl")
    elif dangling and not hero:
        # Every photograph is gone. Clearing it is what lets the hero fall back
        # to its designed no-photograph state instead of a broken image icon.
        draft["heroImageUrl"] = None

    if not draft.get("design"):
        draft["design"] = {"font": "modern", "buttons": "rounded", "layout": "standard"}

    # A complete shape, whatever the hotel had. A draft missing `faq` entirely
    # and one holding `[]` mean the same thing to every reader, and only one of
    # them survives a `KeyError` in a caller that forgot `.get`.
    for key, blank in (
        ("about", ""), ("highlights", []), ("amenities", []),
        ("faq", []), ("offers", []), ("seo", {}), ("gallery", []),
    ):
        draft.setdefault(key, blank)

    # Sections: on where there is something to draw, off where there is not.
    #
    # This is the half that makes the page look designed rather than
    # generated. `harry` had all eight switched on and content for none of
    # them, which is a home page of headings over nothing.
    has = {
        "hero": True,
        "highlights": bool(draft.get("highlights")),
        "rooms": bool(found.get("rooms")),
        "about": bool((draft.get("about") or "").strip()),
        "amenities": bool(draft.get("amenities")),
        "gallery": len(gallery) >= 3,
        "experiences": bool(found.get("experiences")),
        "offers": bool(draft.get("offers")),
        "location": bool(prop.get("address") or prop.get("lat")),
        "reviews": False,
        "faq": bool(draft.get("faq")),
        "policies": bool(prop.get("policies")),
        "contact": bool(draft.get("contactPhone") or draft.get("contactEmail")),
    }
    draft["sections"] = [
        {"id": s, "enabled": has.get(s, False)} for s in website_service.SECTIONS
    ]

    draft["generatedAt"] = datetime.now(timezone.utc).isoformat()
    draft[_AUTHORED_KEY] = sorted(set(written))
    return draft


def template_for(found: dict, current: str | None) -> str | None:
    """A look to start from, only for a hotel that has never picked one."""
    if current and current != "boutique":
        return None
    kind = (found.get("property") or {}).get("property_type") or "hotel"
    return _TEMPLATE_FOR.get(kind, "boutique")


def accent_for(template: str) -> str:
    return _ACCENT_FOR.get(template, "#0F766E")
