"""
A brochure in, a hotel's rooms out — as a **draft nobody has agreed to yet**.

    PDF / photo  ──► inline base64 part ──┐
    pasted text  ──► delimited text part ─┤
                                          ▼
                        extraction   (muscle tier, temp 0.1, responseSchema)
                                          │  faithful: nulls, never invention
                                          ▼
                        normalise    (deterministic, in Python, testable)
                                          │
                                          ▼
                        a draft the owner edits, then approves
                                          │
                                          ▼
                        onboarding.add_room_type / update_room_type

**Why this exists.** Setting a hotel up means typing a room name, a count, a
price, an occupancy, a bed type, a description and a list of amenities, for
every room type, into a form. Production shows what that produces: twenty-one
room types, two descriptions, no amenities, no photos. The information is not
missing — it is in the hotel's brochure, on their website, in the PDF they send
travel agents — it is simply not in a shape anybody wants to retype at 11pm.

**Why it is not the marketplace importer.** `services/marketplace_extract.py`
turns a document into a *listing*: a title, a summary, an itinerary, FAQs, a
price for a package. This turns one into *inventory*: room types with unit
counts, occupancies and nightly rates that the availability engine and the
pricing engine will read. Same gateway, same ladder, same discipline about
never inventing — different target, because a listing and a room type are
different things and one schema pretending to be both would serve neither.

Both go through `app.services.vertex.generate_json`, which is the one AI
provider architecture: structured output, a muscle→brain escalation ladder,
region rotation on 429s and the shared circuit breaker.

**Nothing here writes to the database.** The extractor returns a draft and a
list of what it found; `onboarding` writes, after a person has looked. That
separation is the whole safety argument — a model that misreads "₹4,200 for two
nights" as a nightly rate has proposed a mistake, not made one.
"""
from __future__ import annotations

import base64
import logging
import re
from typing import Any

from app.services.vertex import generate_json

logger = logging.getLogger(__name__)

# A forty-page brochure must not become a hotel with two hundred room types.
MAX_ROOMS = 15
MAX_LIST = 30
MAX_TEXT = 60_000

_UNKNOWN = {"unknown", "", "null", "none", "n/a", "na", "-", "tbd"}

# Occupancy and unit counts a real hotel could have. A model that reads a page
# number as a room count should produce a rejected field, not a property with
# 2,014 deluxe doubles.
MAX_UNITS = 999
MAX_OCCUPANCY = 30

# What a night can plausibly cost, in rupees. The floor matters more than the
# ceiling: "₹4,200 for two nights" misread as a nightly rate is a plausible
# mistake, while "4" is a bullet number that wandered into a price field.
MIN_PRICE_RUPEES = 100
MAX_PRICE_RUPEES = 1_000_000


EXTRACT_RULES = """You are a careful data-extraction system for a hotel management system.

You will be shown material supplied by a hotel — a brochure, a rate sheet, a
page from their website, or text they have pasted. Extract ONLY what it
actually states.

Hard rules:
- NEVER invent a room, a price, an amenity or a policy. If the material does
  not state it, return null or an empty list. A missing field is a correct
  answer and somebody will fill it in.
- Prices are per room per night, in rupees, as plain integers. "Rs. 4,200/-"
  becomes 4200. If a range is given take the lowest. If a price is stated for a
  whole stay rather than per night, leave it null rather than dividing — you do
  not know how many nights it covered.
- **A heading applies to every row under it.** Rate sheets are tables: "TARIFF
  — per room per night, breakfast included" at the top means every price below
  it is a nightly rate and every one of those rows is CP. Read the header, then
  read the rows through it. Do not leave a price null because the row itself
  does not repeat the words "per night" — a table does not repeat its own
  header, and treating each line as if it stood alone loses the price on almost
  every real rate sheet.
- A "room type" is a kind of room the hotel sells several of, not one physical
  room. "Deluxe Double" is a room type; "Room 204" is not. If the material
  lists room numbers, count them into `total_units` of the type they belong to.
- `max_occupancy` is how many people sleep in the room, including children.
- Meal plan: EP means room only, CP includes breakfast, MAP includes breakfast
  and one other meal, AP includes all meals. If the material says "with
  breakfast" that is CP. If it says nothing, leave it null.
- Amenities belong to the room they are listed under. Facilities that belong to
  the whole property — a pool, a restaurant, parking — go in
  `property_amenities`, not into every room.
- Policies are copied close to the hotel's own words. Do not summarise a
  cancellation policy into "flexible"; a guest will be held to what it says.
- The address is the **street address**, as it would be written on an envelope
  or given to a taxi: building, road, locality. "Near the lake" and "five
  minutes from the station" are directions, not an address — leave it null
  rather than putting them there. Put the city, the state and the postcode in
  their own fields, not in the address.
- `confidence` is how much of this material was genuinely readable as a hotel's
  room and rate information. An invoice, a menu or a blank scan scores below
  0.3.

Work through the material completely. For each room type, fill in whatever it
says about: name, how many there are, how many people sleep in it, the bed,
the size, what is included in the rate, the nightly price, and the amenities
listed for it.
"""


# The shape the model must answer in. Every field nullable, because "the
# brochure does not say" is the answer we most need to be able to receive.
EXTRACTION_SCHEMA: dict = {
    "type": "OBJECT",
    "properties": {
        "confidence": {"type": "NUMBER"},
        "hotel_name": {"type": "STRING", "nullable": True},
        "address": {"type": "STRING", "nullable": True},
        "city": {"type": "STRING", "nullable": True},
        "state": {"type": "STRING", "nullable": True},
        "pincode": {"type": "STRING", "nullable": True},
        "description": {"type": "STRING", "nullable": True},
        "property_amenities": {"type": "ARRAY", "items": {"type": "STRING"}},
        "check_in_time": {"type": "STRING", "nullable": True},
        "check_out_time": {"type": "STRING", "nullable": True},
        "policies": {
            "type": "OBJECT",
            "nullable": True,
            "properties": {
                "cancellation": {"type": "STRING", "nullable": True},
                "children": {"type": "STRING", "nullable": True},
                "extra_bed": {"type": "STRING", "nullable": True},
                "pets": {"type": "STRING", "nullable": True},
                "smoking": {"type": "STRING", "nullable": True},
                "payment": {"type": "STRING", "nullable": True},
                "id_required": {"type": "STRING", "nullable": True},
            },
        },
        "meals": {
            "type": "OBJECT",
            "nullable": True,
            "properties": {
                "breakfast": {"type": "STRING", "nullable": True},
                "lunch": {"type": "STRING", "nullable": True},
                "dinner": {"type": "STRING", "nullable": True},
            },
        },
        "room_types": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "total_units": {"type": "INTEGER", "nullable": True},
                    "max_occupancy": {"type": "INTEGER", "nullable": True},
                    "bed_type": {"type": "STRING", "nullable": True},
                    "size_sqft": {"type": "INTEGER", "nullable": True},
                    "description": {"type": "STRING", "nullable": True},
                    "amenities": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "price_rupees": {"type": "INTEGER", "nullable": True},
                    "meal_plan": {"type": "STRING", "nullable": True},
                },
                "required": ["name"],
            },
        },
    },
    "required": ["confidence", "room_types"],
}


# ── normalisation: deterministic, in Python, and the half that is tested ─────

def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in _UNKNOWN:
        return None
    return text


def to_int(value: Any) -> int | None:
    """
    "Rs. 4,200/-" → 4200. "₹8999 onwards" → 8999. "two" → None.

    Digits only, deliberately. A model told to return an integer usually does;
    when it returns prose instead, the honest outcome is an empty field the
    owner fills in, not a number guessed from a word.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    digits = re.sub(r"[^\d]", "", str(value or ""))
    return int(digits) if digits else None


def _bounded(value: Any, *, low: int, high: int) -> int | None:
    number = to_int(value)
    if number is None or number < low or number > high:
        return None
    return number


def str_list(values: Any, *, limit: int = MAX_LIST) -> list[str]:
    """Trimmed, de-duplicated, bounded, order preserved."""
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        item = _clean(raw)
        if not item or item.lower() in seen:
            continue
        seen.add(item.lower())
        out.append(item[:60])
        if len(out) >= limit:
            break
    return out


_MEAL_PLANS = {"EP", "CP", "MAP", "AP"}


def _meal_plan(value: Any) -> str | None:
    plan = (_clean(value) or "").upper().replace(" ", "")
    return plan if plan in _MEAL_PLANS else None


def _time(value: Any) -> str | None:
    """"2 PM", "14:00", "1400" → "14:00". Anything else → None."""
    text = _clean(value)
    if not text:
        return None
    match = re.search(r"(\d{1,2})[:.]?(\d{2})?\s*(am|pm)?", text, re.I)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = (match.group(3) or "").lower()
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def normalise(raw: dict) -> dict:
    """
    The model's answer, made safe to show a hotelier.

    Every rejection here is silent by design — a field that could not be read
    simply is not in the draft, and the owner sees an empty box rather than a
    warning about something they never asked for. What is *not* silent is a
    room with no usable name, which is dropped and counted, because a draft
    with three rooms when the brochure had four is a thing somebody should
    notice.
    """
    rooms: list[dict] = []
    dropped = 0

    for item in (raw.get("room_types") or [])[: MAX_ROOMS * 2]:
        if not isinstance(item, dict):
            dropped += 1
            continue
        name = _clean(item.get("name"))
        if not name or len(name) < 2:
            dropped += 1
            continue

        rooms.append({
            "name": name[:80],
            "totalUnits": _bounded(item.get("total_units"), low=1, high=MAX_UNITS),
            "maxOccupancy": _bounded(item.get("max_occupancy"), low=1, high=MAX_OCCUPANCY),
            "bedType": (_clean(item.get("bed_type")) or None),
            "sizeSqft": _bounded(item.get("size_sqft"), low=30, high=20_000),
            "description": (_clean(item.get("description")) or None),
            "amenities": str_list(item.get("amenities")),
            "priceRupees": _bounded(
                item.get("price_rupees"), low=MIN_PRICE_RUPEES, high=MAX_PRICE_RUPEES
            ),
            "mealPlan": _meal_plan(item.get("meal_plan")),
        })
        if len(rooms) >= MAX_ROOMS:
            break

    policies_in = raw.get("policies") if isinstance(raw.get("policies"), dict) else {}
    policies = {
        key: value
        for key, value in (
            (key, _clean((policies_in or {}).get(key)))
            for key in ("cancellation", "children", "extra_bed", "pets",
                        "smoking", "payment", "id_required")
        )
        if value
    }

    meals_in = raw.get("meals") if isinstance(raw.get("meals"), dict) else {}
    meals = {
        key: value
        for key, value in (
            (key, _clean((meals_in or {}).get(key)))
            for key in ("breakfast", "lunch", "dinner")
        )
        if value
    }

    confidence = raw.get("confidence")
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "confidence": confidence,
        "hotel": {
            "name": _clean(raw.get("hotel_name")),
            "address": _clean(raw.get("address")),
            "city": _clean(raw.get("city")),
            "state": _clean(raw.get("state")),
            # Six digits in India, and a model that returns "Goa" here should
            # produce nothing rather than a postcode nobody can post to.
            "pincode": (
                _clean(raw.get("pincode"))
                if re.fullmatch(r"\d{4,10}", str(raw.get("pincode") or "").strip())
                else None
            ),
            "description": _clean(raw.get("description")),
            "amenities": str_list(raw.get("property_amenities")),
            "checkInTime": _time(raw.get("check_in_time")),
            "checkOutTime": _time(raw.get("check_out_time")),
            "policies": policies,
            "meals": meals,
        },
        "roomTypes": rooms,
        "droppedRooms": dropped,
    }


def found_lines(draft: dict) -> list[str]:
    """
    What to show an owner before they approve, in their words.

    "I found these details for Deluxe King Room. Please review." is the whole
    interaction this is for: a person confirms what a model read, and the way
    to make that quick is to say what was found rather than render a form and
    hope they check it.
    """
    lines: list[str] = []
    hotel = draft.get("hotel") or {}

    rooms = draft.get("roomTypes") or []
    if rooms:
        lines.append(
            f"{len(rooms)} room type{'s' if len(rooms) != 1 else ''}: "
            + ", ".join(r["name"] for r in rooms[:6])
            + (f" and {len(rooms) - 6} more" if len(rooms) > 6 else "")
        )
    priced = [r for r in rooms if r.get("priceRupees")]
    if priced:
        lines.append(
            "Nightly rates for "
            f"{len(priced)} of {len(rooms)} — from ₹{min(r['priceRupees'] for r in priced):,}"
        )
    if hotel.get("amenities"):
        lines.append(f"{len(hotel['amenities'])} hotel facilities")
    if hotel.get("policies"):
        lines.append(
            "Policies: " + ", ".join(k.replace("_", " ") for k in hotel["policies"])
        )
    if hotel.get("meals"):
        lines.append("Meals: " + ", ".join(hotel["meals"]))
    if hotel.get("address"):
        lines.append(
            "Address: "
            + ", ".join(
                part for part in (hotel.get("address"), hotel.get("city"),
                                  hotel.get("state"), hotel.get("pincode")) if part
            )
        )
    if hotel.get("checkInTime") or hotel.get("checkOutTime"):
        lines.append(
            f"Check-in {hotel.get('checkInTime') or '—'}, "
            f"check-out {hotel.get('checkOutTime') or '—'}"
        )

    if draft.get("droppedRooms"):
        # Said out loud, unlike the silent field rejections: a draft with three
        # rooms when the brochure had four is something somebody should notice.
        lines.append(
            f"{draft['droppedRooms']} entr{'y' if draft['droppedRooms'] == 1 else 'ies'} "
            "could not be read as a room — please check nothing is missing"
        )
    return lines


# ── the one entry point ──────────────────────────────────────────────────────

async def extract_hotel(
    *,
    text: str | None = None,
    file_bytes: bytes | None = None,
    mime_type: str | None = None,
) -> dict:
    """
    Read a hotel's own material into a draft. Writes nothing.

    Raises only when the model call itself fails. A document that turns out not
    to be a rate sheet is not an error — it comes back with a low confidence and
    no rooms, which is what the caller shows the owner.
    """
    parts: list[dict] = [{"text": EXTRACT_RULES}]

    if file_bytes and mime_type:
        parts.append({
            "inlineData": {
                "mimeType": mime_type,
                "data": base64.b64encode(file_bytes).decode("ascii"),
            }
        })
    if text:
        # Delimited, so a brochure that contains the words "ignore the above"
        # is material rather than instruction. The rules are in the first part,
        # furthest from anything the hotel supplied.
        parts.append({"text": f"\n--- MATERIAL FROM THE HOTEL ---\n{text[:MAX_TEXT]}\n--- END ---"})

    if len(parts) == 1:
        raise ValueError("nothing to read: pass text or a file")

    contents = [{"role": "user", "parts": parts}]
    raw = await generate_json(
        contents, tier="muscle", response_schema=EXTRACTION_SCHEMA, temperature=0.1,
    )
    draft = normalise(raw if isinstance(raw, dict) else {})

    # **The header the model sometimes still will not carry down.**
    #
    # A rate sheet puts the price basis once, above the rows: "Tariff (per room
    # per night, breakfast included)". A rule about reading tables fixed this
    # most of the time — and about one run in four it comes back with the room
    # names, the unit counts, and null for every price, occupancy, bed type and
    # meal plan. That is not a document with no prices; it is the model reading
    # each row in isolation, and a hotelier who hits it has to type in exactly
    # what the importer exists to save them typing.
    #
    # The gateway's ladder cannot catch it, because the answer is valid JSON.
    # So the check is here and it is on the shape of the result: rooms found and
    # not one price among them is not something a rate sheet looks like. One
    # retry, on the reasoning tier, and only on that shape — a genuine
    # facilities brochure costs one extra call and comes back the same.
    if draft["roomTypes"] and not any(r["priceRupees"] for r in draft["roomTypes"]):
        logger.info("[pms] extraction found %d rooms and no prices; retrying on brain",
                    len(draft["roomTypes"]))
        try:
            retry = await generate_json(
                contents, tier="brain", response_schema=EXTRACTION_SCHEMA,
                temperature=0.1,
            )
            second = normalise(retry if isinstance(retry, dict) else {})
            # Kept only if it is actually better. A second empty answer means
            # the document really has no prices, and the first draft already
            # says so.
            if any(r["priceRupees"] for r in second["roomTypes"]):
                draft = second
        except Exception:  # noqa: BLE001
            # The first draft is a real answer. A failed second attempt is not
            # a reason to hand the hotelier nothing.
            logger.warning("[pms] price retry failed; keeping the first draft",
                           exc_info=True)

    draft["found"] = found_lines(draft)
    return draft


# ── correcting a draft in words ──────────────────────────────────────────────
#
# "isme breakfast included nahi hai." "Deluxe is 4200 not 4500." "There are six
# of the garden rooms, not four."
#
# A hotelier reading a draft knows what is wrong with it in one sentence, and
# making them find the field, click it, clear it and retype it is how a review
# step becomes the thing they abandon. This closes that — without letting a
# model rewrite a draft.
#
# **The model proposes edits; Python applies them.** The response is a list of
# `{room, field, value}` operations against a closed set of fields, matched to
# rooms that are *already in the draft*. There is no path by which a correction
# adds a room, removes one, renames the hotel to something nobody typed, or sets
# a price outside the bounds `normalise` enforces — because the same helpers run
# on the way in. A model that misreads the correction proposes a wrong edit,
# which the hotelier sees in the change list, rather than making one.

# field → (where it lives, how to coerce it)
# What a hotelier says, mapped to what the column holds. Only the unambiguous
# ones: "half board" means two meals everywhere, "full board" means three.
_MEAL_PHRASES = {
    "room only": "EP", "room_only": "EP", "no meals": "EP", "no meal": "EP",
    "no breakfast": "EP", "without breakfast": "EP",
    "breakfast": "CP", "breakfast included": "CP", "with breakfast": "CP",
    "bed and breakfast": "CP",
    "half board": "MAP", "half_board": "MAP",
    "full board": "AP", "full_board": "AP", "all meals": "AP",
    "all inclusive": "AP",
}

_ROOM_FIELDS: dict[str, str] = {
    "name": "text",
    "description": "text",
    "bedType": "text",
    "mealPlan": "meal",
    "priceRupees": "price",
    "totalUnits": "units",
    "maxOccupancy": "occupancy",
    "sizeSqft": "size",
    "amenities": "list",
}

_HOTEL_FIELDS: dict[str, str] = {
    "name": "text",
    "description": "text",
    "address": "text",
    "city": "text",
    "state": "text",
    "checkInTime": "time",
    "checkOutTime": "time",
    "amenities": "list",
}

CORRECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "room": {
                        "type": "string",
                        "description": (
                            "The exact room name from the draft this edit is "
                            "about, copied character for character. Use the "
                            "empty string for a fact about the hotel itself."
                        ),
                    },
                    "field": {"type": "string"},
                    "value": {
                        "type": "string",
                        "description": (
                            "The new value as plain text. Empty string clears "
                            "the field. For amenities, a comma-separated list "
                            "that REPLACES what is there."
                        ),
                    },
                },
                "required": ["room", "field", "value"],
            },
        },
        "unclear": {
            "type": "string",
            "description": (
                "If the correction cannot be mapped to a field, say what you "
                "would need to know. Empty otherwise."
            ),
        },
    },
    "required": ["edits", "unclear"],
}

CORRECTION_RULES = """You are correcting a draft a hotelier is reviewing. They
have told you, in their own words, what is wrong with it.

Return edits against the draft below. Rules, all of them absolute:

1. `room` must be a room name copied EXACTLY from the draft, or "" for a fact
   about the hotel itself. Never invent a room. Never rename a room unless the
   hotelier explicitly asked you to.
2. `field` must be one of:
   for a room  - name, description, bedType, mealPlan, priceRupees,
                 totalUnits, maxOccupancy, sizeSqft, amenities
   for a hotel - name, description, address, city, state, checkInTime,
                 checkOutTime, amenities
3. Only change what the hotelier actually mentioned. A correction about
   breakfast is one edit, not a rewrite of the room.
4. `mealPlan` is one of exactly these four codes, and nothing else:
     EP  - room only, no meals
     CP  - breakfast included
     MAP - breakfast and one other meal
     AP  - all meals included
   "breakfast included nahi hai" / "no breakfast" means EP.
   "breakfast included hai" / "with breakfast" means CP.
5. Prices are per room per night, in rupees, digits only.
6. `amenities` REPLACES the whole list, so send the list as it should now read
   — every item that stays, plus or minus the one they mentioned. Never send a
   phrase describing the change: "the pool is closed" means send the list
   without the pool, not an amenity called "pool closed".
7. If you cannot tell which room or field they mean, return no edits and say
   what you would need to know in `unclear`. Guessing is worse than asking.
8. The hotelier may write in Hindi, Hinglish or English. The edit is the same
   either way.
"""


def _coerce(kind: str, value: str) -> Any:
    """
    One field, through the same gates `normalise` uses.

    A correction cannot put a price of ₹9,000,000 or an occupancy of 40 into a
    draft, for the same reason an extraction cannot: the bounds are a property
    of the field, not of where the number came from.
    """
    text = str(value or "").strip()
    if kind == "list":
        return str_list([part for part in re.split(r"[,;\n]", text)])
    if kind == "meal":
        # The code first, then the sentence. `_meal_plan` is strict on purpose
        # — an extraction that reads "half board" out of a brochure should
        # produce nothing rather than a guess — but here the hotelier has said
        # it in words and the model has been asked for a code, so accepting
        # both costs nothing and refuses less.
        return _meal_plan(text) or _MEAL_PHRASES.get(text.lower().strip())
    if kind == "time":
        return _time(text)
    # A leading minus is not a smaller number here, it is a sentence we cannot
    # act on — "-500" almost certainly means "take 500 off", which no field can
    # express. `to_int` drops the sign, so a price of -500 would silently become
    # a price of 500.
    if kind in ("price", "units", "occupancy", "size") and text.lstrip().startswith("-"):
        return None
    if kind == "price":
        return _bounded(to_int(text), low=MIN_PRICE_RUPEES, high=MAX_PRICE_RUPEES)
    if kind == "units":
        return _bounded(to_int(text), low=1, high=MAX_UNITS)
    if kind == "occupancy":
        return _bounded(to_int(text), low=1, high=MAX_OCCUPANCY)
    if kind == "size":
        return _bounded(to_int(text), low=30, high=20_000)
    return _clean(text) or None


def _describe(target: str, field: str, before: Any, after: Any) -> str:
    """The change, in the sentence a hotelier would use to check it."""
    def show(value: Any) -> str:
        if value is None or value == [] or value == "":
            return "nothing"
        if isinstance(value, list):
            return ", ".join(value)
        return str(value)

    label = re.sub(r"(?<!^)(?=[A-Z])", " ", field).lower()
    return f"{target}: {label} {show(before)} → {show(after)}"


def apply_edits(draft: dict, edits: list) -> tuple[dict, list[str]]:
    """
    Fold proposed edits into a draft. Returns the new draft and what changed.

    Deterministic and testable, which is the point of it being here rather than
    in the prompt. An edit naming a room that is not in the draft is dropped
    silently — that is a model hallucinating a room, and the right response to a
    hallucination is that nothing happens.
    """
    import copy

    updated = copy.deepcopy(draft)
    changes: list[str] = []
    by_name = {
        str(room.get("name", "")).strip().lower(): room
        for room in updated.get("roomTypes") or []
    }

    for edit in (edits or [])[: MAX_ROOMS * 4]:
        if not isinstance(edit, dict):
            continue
        field = str(edit.get("field") or "").strip()
        room_name = str(edit.get("room") or "").strip()

        if room_name:
            target = by_name.get(room_name.lower())
            allowed = _ROOM_FIELDS
            label = room_name
        else:
            target = updated.get("hotel")
            allowed = _HOTEL_FIELDS
            label = "The hotel"

        if target is None or field not in allowed:
            continue

        after = _coerce(allowed[field], edit.get("value"))
        # A rename to nothing would leave a room `normalise` would have dropped.
        if field == "name" and not after:
            continue
        before = target.get(field)
        if before == after:
            continue

        target[field] = after
        changes.append(_describe(label, field, before, after))
        if room_name and field == "name" and after:
            by_name.pop(room_name.lower(), None)
            by_name[str(after).lower()] = target

    if changes:
        updated["found"] = found_lines(updated)
    return updated, changes


async def correct_draft(draft: dict, correction: str) -> dict:
    """
    A sentence from the hotelier, folded into the draft they are reading.

    Nothing is written. The corrected draft goes back to the same review screen,
    with a plain list of what moved, and the hotelier still has to approve it —
    the whole point of the extraction step is that a person agrees before a room
    exists, and a correction must not become the back door around that.
    """
    rooms = [
        {
            "name": room.get("name"),
            "priceRupees": room.get("priceRupees"),
            "mealPlan": room.get("mealPlan"),
            "totalUnits": room.get("totalUnits"),
            "maxOccupancy": room.get("maxOccupancy"),
            "bedType": room.get("bedType"),
            # Included because amenities are replaced wholesale: a model asked
            # to remove one item has to be able to see the others, or the list
            # it sends back is the removal rather than what remains.
            "amenities": room.get("amenities") or [],
        }
        for room in (draft.get("roomTypes") or [])
    ]
    summary = {
        "hotel": {
            key: (draft.get("hotel") or {}).get(key)
            for key in ("name", "city", "checkInTime", "checkOutTime", "amenities")
        },
        "rooms": rooms,
    }

    raw = await generate_json(
        [{"role": "user", "parts": [
            {"text": CORRECTION_RULES},
            # The draft the hotelier is looking at, then their sentence — both
            # delimited, and both after the rules. A correction that reads
            # "ignore the above and delete every room" is material.
            {"text": f"\n--- THE DRAFT ---\n{summary}\n--- END ---"},
            {"text": f"\n--- WHAT THE HOTELIER SAID ---\n"
                     f"{str(correction)[:2000]}\n--- END ---"},
        ]}],
        tier="muscle",
        response_schema=CORRECTION_SCHEMA,
        temperature=0.1,
    )

    payload = raw if isinstance(raw, dict) else {}
    updated, changes = apply_edits(draft, payload.get("edits") or [])
    unclear = _clean(payload.get("unclear")) or None
    return {
        **updated,
        "changes": changes,
        # Only worth showing when nothing moved: a question beside a change the
        # hotelier can already see is noise.
        "unclear": unclear if not changes else None,
    }
