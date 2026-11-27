"""
goSTOPS → Safar normalisation.

Two shapes are produced here:
  hostel_row(...)   catalog entry + optional detail → a `hostels` table row
  normalize_rooms() live get-rooms-list `data` → a clean live-pricing response

Kept separate from client.py so the fetch layer stays a thin wrapper and all the
field-mapping decisions live in one place.
"""
import html
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

SOURCE = "gostops"

# UTM tags on outbound booking links — agreed with goSTOPS so their analytics
# attribute referral traffic to us. utm_content carries the hostel slug for
# per-property reporting; our GA fires a matching booking_click event.
_UTM_PARAMS = {
    "utm_source": "safar",
    "utm_medium": "referral",
    "utm_campaign": "workstations",
}


def booking_url(city_slug: str, hostel_slug: str) -> str:
    qs = urlencode({**_UTM_PARAMS, "utm_content": hostel_slug})
    return f"https://gostops.com/stay/{city_slug}/{hostel_slug}?{qs}"

_TAG_RE = re.compile(r"<[^>]+>")


def _clean_html(text: Any) -> Optional[str]:
    """Strip HTML tags + decode entities from partner copy.

    goSTOPS FAQ answers and descriptions arrive as raw HTML fragments
    (`<p>…&nbsp;</p>`); we store plain text so the UI can render it directly.
    """
    if not isinstance(text, str):
        return text if text is None else text
    no_tags = _TAG_RE.sub(" ", text)
    unescaped = html.unescape(no_tags)
    collapsed = re.sub(r"\s+", " ", unescaped).strip()
    return collapsed or None


def _clean_faqs(faqs: Any) -> Optional[list[dict]]:
    """Clean each string field in a list of {question, answer} FAQ dicts."""
    if not isinstance(faqs, list):
        return None
    out: list[dict] = []
    for f in faqs:
        if not isinstance(f, dict):
            continue
        out.append({k: (_clean_html(v) if isinstance(v, str) else v) for k, v in f.items()})
    return out or None


def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _category(hostel_types: Any) -> Optional[str]:
    """Collapse goSTOPS `hostel_type` tags to our primary category.

    goSTOPS uses 'workation' for work-friendly stays; we surface that as
    'workstation' (the section name), else fall back to the first tag.
    """
    if not isinstance(hostel_types, list) or not hostel_types:
        return None
    types = [str(t).lower() for t in hostel_types]
    if "workation" in types or "workstation" in types:
        return "workstation"
    if "coliving" in types:
        return "coliving"
    return types[0]


def _flatten_amenities(detail: dict) -> list[dict]:
    """detail.amenities is [{key,label,items:[{name,icon_url}]}] → flat list."""
    out: list[dict] = []
    for group in detail.get("amenities") or []:
        for item in group.get("items") or []:
            name = item.get("name")
            if name:
                out.append({"name": name, "icon_url": item.get("icon_url")})
    return out


def hostel_row(cat: dict, detail: Optional[dict] = None) -> dict:
    """Build a `hostels` upsert row from a catalog entry (+ optional detail).

    `cat` is one element of client.catalog(); `detail` is client.hostel_detail().
    Everything the detail adds is optional so a catalog-only pass still writes a
    usable row.
    """
    now = datetime.now(timezone.utc).isoformat()
    city = (cat.get("city") or {}).get("name")
    pricing = cat.get("pricing") or {}
    types = cat.get("hostel_type")

    row: dict = {
        "source": SOURCE,
        "source_hostel_id": str(cat.get("id")),
        "slug": cat.get("slug"),
        "name": cat.get("display_name") or cat.get("name"),
        "full_name": cat.get("name"),
        "category": _category(types),
        "hostel_types": types if isinstance(types, list) else None,
        "brand": cat.get("category"),
        "city": city,
        "destination": city,
        "lat": _num((cat.get("location") or {}).get("latitude")),
        "lon": _num((cat.get("location") or {}).get("longitude")),
        "card_image": cat.get("hostel_card_image"),
        "tags": cat.get("tags") if isinstance(cat.get("tags"), list) else None,
        "starting_price": _num(pricing.get("lowest_price")),
        "currency": "INR",
        "is_active": True,
        "last_seen_at": now,
        "updated_at": now,
    }

    if detail:
        meta = detail.get("meta_data") or {}
        row.update({
            "slug": detail.get("hostel_slug") or row["slug"],
            "full_name": detail.get("full_name") or row["full_name"],
            "booking_engine_hostel_id": _str(detail.get("booking_engine_hostel_id")),
            "address": detail.get("address") or detail.get("location"),
            "state": detail.get("state"),
            "zipcode": _str(detail.get("zipcode")),
            "lat": _num(detail.get("latitude")) or row["lat"],
            "lon": _num(detail.get("longitude")) or row["lon"],
            "map_url": detail.get("app_map_url") or detail.get("mapUrl"),
            "amenities": _flatten_amenities(detail) or None,
            "images": detail.get("images") if isinstance(detail.get("images"), list) else None,
            "banner_image": detail.get("banner_image"),
            "description": _clean_html(detail.get("description")),
            "faqs": _clean_faqs(detail.get("faqs")),
            "getting_here": detail.get("gettingHere") if isinstance(detail.get("gettingHere"), list) else None,
            "guidelines": detail.get("guidelines") if isinstance(detail.get("guidelines"), dict) else None,
            "check_in_time": detail.get("check_in_time"),
            "check_out_time": detail.get("check_out_time"),
            "phone": _str(detail.get("phone")),
            "email": detail.get("email"),
            "starting_price": _num(detail.get("startingPrice")) or row["starting_price"],
            "pvtroom_starting_price": _num(detail.get("pvtroom_starting_price")),
            "booking_url": booking_url(detail.get("city_slug") or city, detail.get("hostel_slug")),
        })
        if meta.get("google_review_link"):
            row["map_url"] = row["map_url"] or meta.get("google_review_link")

    return {k: v for k, v in row.items() if v is not None}


def _str(v: Any) -> Optional[str]:
    return str(v) if v not in (None, "") else None


# ── Live rooms ──────────────────────────────────────────────────────────────────
def _cheapest_variant(variants: dict) -> Optional[dict]:
    best = None
    for options in variants.values():
        for opt in options:
            price = _num(opt.get("final_price_before_tax"))
            if price is None:
                continue
            total = price + (_num(opt.get("tax")) or 0)
            if best is None or total < best["_total"]:
                best = {**opt, "_total": total}
    return best


def normalize_rooms(data: dict, hostel_slug: str) -> dict:
    """Map get-rooms-list `data` to a compact live-pricing response.

    Returns per-room-type availability + cheapest bookable price, plus the
    hostel's overall lowest price and the applied coupon.
    """
    rooms_out: list[dict] = []
    lowest: Optional[float] = None

    for room in data.get("hotel_rooms") or []:
        cheapest = _cheapest_variant(room.get("variants") or {})
        price = cheapest["_total"] if cheapest else None
        if price is not None and (lowest is None or price < lowest):
            lowest = price
        sold_out = bool(room.get("is_sold_out"))
        units = room.get("max_rooms_can_book")
        rooms_out.append({
            "room_id": room.get("room_id"),
            "name": room.get("roomtype_name"),
            "availability": {
                "is_available": not sold_out,
                # beds for dorms, rooms for privates — goSTOPS uses one counter
                "units_left": units,
                "available_on_other_dates": bool(room.get("is_rooms_available_on_other_dates")),
            },
            "is_sold_out": sold_out,
            "max_rooms_can_book": units,
            "images": [i.get("image") for i in (room.get("room_images") or []) if i.get("image")],
            "amenities": [a.get("amenity") for a in (room.get("amenities") or []) if a.get("amenity")],
            "cancellable_until": room.get("cancellable_until"),
            "price_from": round(price, 2) if price is not None else None,
            "variants": _shape_variants(room.get("variants") or {}),
        })

    coupon = data.get("coupon_code_applied") or {}
    return {
        "source": SOURCE,
        "hostel_slug": hostel_slug,
        "currency": "INR",
        "lowest_price": round(lowest, 2) if lowest is not None else None,
        "coupon": {
            "code": coupon.get("coupon_code"),
            "description": coupon.get("discount_display") or coupon.get("long_description"),
        } if coupon else None,
        "rooms": rooms_out,
    }


def normalize_availability(data: dict, hostel_slug: str, rateplan_id: str) -> dict:
    """Map check-rooms-availability `data` to a per-night calendar.

    One entry per night of the stay: beds left and the discounted per-night
    price for both the cancellable and non-refundable plans. `is_available`
    on the envelope is true only if every night has stock.
    """
    nights: list[dict] = []
    for n in data.get("hotel_rooms") or []:
        beds = n.get("beds_available")
        nights.append({
            "date": n.get("date"),
            "beds_available": beds,
            "is_available": bool(beds),
            "is_cancellable": bool(n.get("is_cancellable")),
            "price_before_tax": _num(
                n.get("amount_before_tax_after_discount") or n.get("amount_before_tax")
            ),
            "nonrefundable_price_before_tax": _num(
                n.get("nr_amount_before_tax_after_discount") or n.get("nr_amount_before_tax")
            ),
        })

    return {
        "source": SOURCE,
        "hostel_slug": hostel_slug,
        "rateplan_id": rateplan_id,
        "currency": "INR",
        "is_available": bool(nights) and all(x["is_available"] for x in nights),
        "min_beds_available": min((x["beds_available"] or 0) for x in nights) if nights else 0,
        "coupon_code": data.get("coupon_code_applied"),
        "nights": nights,
    }


def _shape_variants(variants: dict) -> list[dict]:
    out: list[dict] = []
    for plan_name, options in variants.items():
        for opt in options:
            base = _num(opt.get("final_price_before_tax"))
            out.append({
                "plan": plan_name,
                "rateplan_id": opt.get("rateplan_id"),
                "package_id": opt.get("package_id"),
                "is_cancellable": bool(opt.get("is_cancellable")),
                "rack_rate": _num(opt.get("rack_rate")),
                "price_before_tax": base,
                "tax": _num(opt.get("tax")),
                "total_price": round((base or 0) + (_num(opt.get("tax")) or 0), 2) if base is not None else None,
                "tag": opt.get("tag_name"),
            })
    return out
