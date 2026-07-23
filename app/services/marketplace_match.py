"""
Marketplace matching — turn a trip intent into ranked partner inventory.

This is the piece that makes TravBuddy different from an OTA: the traveller
never searches. The planner already knows the destination, dates, group size,
budget and interests, so the marketplace is consulted *during* planning and the
result is woven into the itinerary.

Design rules, in priority order:

  1. **Relevance is a hard gate, not a ranking signal.** Anything outside the
     destination or over budget is dropped before scoring. An empty result is
     always better than a bad one — the planner renders nothing when we return
     nothing.
  2. **Trust wins.** Ranking NEVER considers commission, subscription plan,
     `is_featured`, or any other paid signal. (The design doc originally
     proposed a featured boost for the marketplace; it is deliberately absent
     here — a paid boost inside an AI recommendation is exactly the OTA
     behaviour we are positioning against.)
  3. **Never invent a reason.** Every `why` line is emitted only by the scorer
     that actually fired, from real values on the row.
  4. **The planner stays dumb.** It renders `sections[]` and `day_matches[]`
     without knowing what a hotel is. Adding restaurants, guides or rentals
     later is a new `kind` here and zero planner changes.

Performance: two indexed Supabase reads with narrow column lists, then pure
Python scoring over a bounded candidate set. No N+1 — partners are joined in
the same select. Results are memoised per destination for a short TTL.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable

from app.db.client import get_db

logger = logging.getLogger(__name__)

# ── tunables ─────────────────────────────────────────────────────────────────

# Weights sum to 1.0. Duration and date only mean something for packages;
# hotels and experiences get a neutral value so they aren't structurally
# penalised against packages.
WEIGHTS = {
    "destination": 0.28,
    "budget": 0.22,
    "interest": 0.15,
    "duration": 0.10,
    "date": 0.07,
    "completeness": 0.08,
    "trust": 0.07,
    "freshness": 0.03,
}
NEUTRAL = 0.6

# Below this, we show nothing rather than something weak.
MIN_CONFIDENCE = 0.55
# "This package already covers your trip" is a strong claim; demand more.
REPLACEMENT_CONFIDENCE = 0.80

# Rough share of a trip budget that each category may consume.
ACCOM_SHARE = 0.40
ACTIVITY_SHARE = 0.25
PACKAGE_TOLERANCE = 1.10

MAX_PACKAGES = 1
MAX_HOTELS = 2
MAX_EXPERIENCES = 3
CANDIDATE_CAP = 120

# Razorpay checkout lands in Phase 1.4. Until then an "instant" listing still
# converts through the enquiry form, so promising "Book now" would be a lie.
# Flip this when checkout ships; nothing else needs to change.
BOOKINGS_LIVE = False

_CACHE_TTL_S = 60.0
_cache: dict[str, tuple[float, list[dict], list[dict]]] = {}
_cache_lock = threading.Lock()

_WORD_RE = re.compile(r"[a-z0-9]+")

# Interest vocabulary → the words that actually appear in listing tags/titles.
INTEREST_SYNONYMS: dict[str, set[str]] = {
    "adventure": {"adventure", "rafting", "paragliding", "trek", "trekking", "zorbing",
                  "bungee", "atv", "climbing", "kayaking", "scuba", "diving", "safari"},
    "nature": {"nature", "valley", "lake", "waterfall", "forest", "mountain", "river",
               "sunrise", "sunset", "camping", "wildlife", "trek"},
    "nightlife": {"nightlife", "cafe", "bar", "party", "music", "live", "brewery"},
    "food": {"food", "culinary", "cooking", "walk", "street", "cafe", "dining"},
    "culture": {"culture", "heritage", "temple", "fort", "palace", "museum", "history"},
    "relaxation": {"relax", "spa", "wellness", "yoga", "retreat", "quiet", "slow"},
    "photography": {"photography", "photo", "scenic", "viewpoint", "sunrise", "sunset"},
    "family": {"family", "kids", "child", "easy"},
    "couple": {"couple", "romantic", "honeymoon", "private"},
}


def _tokens(*values: Any) -> set[str]:
    """Lowercase word set from any mix of strings and string lists."""
    out: set[str] = set()
    for v in values:
        if not v:
            continue
        if isinstance(v, (list, tuple, set)):
            for item in v:
                if isinstance(item, str):
                    out.update(_WORD_RE.findall(item.lower()))
        elif isinstance(v, str):
            out.update(_WORD_RE.findall(v.lower()))
    return out


def _norm_place(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ── request context ──────────────────────────────────────────────────────────

@dataclass
class DayPlan:
    day: int
    title: str = ""
    places: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> set[str]:
        return _tokens(self.title, self.places)


@dataclass
class MatchContext:
    destination: str
    days: int
    travellers: int
    origin: str | None = None
    budget: int | None = None
    budget_is_total: bool = False
    interests: list[str] = field(default_factory=list)
    travel_style: str | None = None
    depart_date: date | None = None
    day_plan: list[DayPlan] = field(default_factory=list)

    # ── derived ──
    @property
    def destination_norm(self) -> str:
        return _norm_place(self.destination)

    @property
    def per_person_budget(self) -> int | None:
        """Indian trip budgets are quoted per person by default ("Manali under
        15k"), so `budget_is_total` must be set explicitly to mean the group."""
        if not self.budget:
            return None
        if self.budget_is_total:
            return max(1, int(self.budget / max(1, self.travellers)))
        return int(self.budget)

    @property
    def nights(self) -> int:
        return max(1, self.days - 1)

    @property
    def rooms(self) -> int:
        return max(1, math.ceil(self.travellers / 2))

    @property
    def interest_tokens(self) -> set[str]:
        out = _tokens(self.interests, self.travel_style)
        for word in list(out):
            out |= INTEREST_SYNONYMS.get(word, set())
        return out

    def nightly_cap(self) -> int | None:
        """Per-room nightly ceiling implied by the budget."""
        pp = self.per_person_budget
        if not pp:
            return None
        total_accom = pp * self.travellers * ACCOM_SHARE
        return max(1, int(total_accom / self.nights / self.rooms))

    def activity_cap(self) -> int | None:
        pp = self.per_person_budget
        return max(1, int(pp * ACTIVITY_SHARE)) if pp else None

    def package_cap(self) -> int | None:
        pp = self.per_person_budget
        return max(1, int(pp * PACKAGE_TOLERANCE)) if pp else None


# ── data access ──────────────────────────────────────────────────────────────

_LISTING_COLS = (
    "id, kind, slug, title, summary, destination, city, photos, price_from, price_unit, "
    "amenities, tags, content, booking_mode, description, updated_at, "
    "partner:partners(brand_name, slug, verified, rating, review_count, response_minutes)"
)

_TRIP_COLS = (
    "id, slug, title, description, destination, origin, duration_days, duration_label, "
    "departure_date, departure_label, seats_left, price_from, hero_image, content, "
    "booking_mode, partner_id, updated_at, "
    "partner:partners(brand_name, slug, verified, rating, review_count, response_minutes)"
)


def _fetch_inventory(destination: str) -> tuple[list[dict], list[dict]]:
    """Published listings + trips for a destination. One query each, joined —
    no N+1. Memoised for _CACHE_TTL_S so a burst of planner runs for the same
    city costs one round-trip."""
    key = _norm_place(destination)
    now = time.time()

    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL_S:
            return hit[1], hit[2]

    pattern = f"%{destination.strip()}%"

    def _listings() -> list[dict]:
        return (
            get_db()
            .table("partner_listings")
            .select(_LISTING_COLS)
            .eq("status", "published")
            .or_(f"destination.ilike.{pattern},city.ilike.{pattern}")
            .limit(CANDIDATE_CAP)
            .execute()
            .data
            or []
        )

    def _trips() -> list[dict]:
        return (
            get_db()
            .table("group_trips")
            .select(_TRIP_COLS)
            .eq("status", "published")
            .ilike("destination", pattern)
            .limit(CANDIDATE_CAP)
            .execute()
            .data
            or []
        )

    # Two independent reads — run them together so a cold city costs one round
    # trip, not two. get_db() is per-thread, so each closure gets its own
    # client rather than sharing one across threads.
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_listings = pool.submit(_listings)
        f_trips = pool.submit(_trips)
        listings, trips = f_listings.result(), f_trips.result()

    with _cache_lock:
        _cache[key] = (now, listings, trips)
        if len(_cache) > 200:  # bounded; planner traffic is long-tail by city
            for stale in [k for k, v in _cache.items() if now - v[0] > _CACHE_TTL_S]:
                _cache.pop(stale, None)

    return listings, trips


# ── scorers ──────────────────────────────────────────────────────────────────
# Each returns (score, why | None). A `why` is emitted ONLY when the signal
# genuinely fired, and only from values present on the row.

def _score_destination(row: dict, ctx: MatchContext) -> tuple[float, str | None]:
    dest = _norm_place(row.get("destination"))
    city = _norm_place(row.get("city"))
    target = ctx.destination_norm
    if not target:
        return NEUTRAL, None

    for value, label in ((dest, row.get("destination")), (city, row.get("city"))):
        if not value:
            continue
        if value == target:
            return 1.0, f"In {label}, exactly where you're headed"
        if target in value or value in target:
            return 0.85, f"In {label}, on your route"
    return 0.0, None


def _score_budget(row: dict, ctx: MatchContext, kind: str) -> tuple[float, str | None]:
    price = row.get("price_from")
    if not price:
        return NEUTRAL, None

    cap = {
        "package": ctx.package_cap(),
        "hotel": ctx.nightly_cap(),
        "experience": ctx.activity_cap(),
    }.get(kind)
    if not cap:
        return NEUTRAL, None

    if price > cap:
        return -1.0, None  # sentinel: caller filters this out

    ratio = price / cap
    score = 1.0 if ratio <= 0.7 else _clamp(1.0 - (ratio - 0.7) / 0.3 * 0.7, 0.3, 1.0)

    if kind == "package":
        why = f"₹{price:,} per person fits your ₹{ctx.per_person_budget:,} budget"
    elif kind == "hotel":
        why = f"₹{price:,} a night sits inside your budget for stays"
    else:
        why = f"₹{price:,} per person leaves room for the rest of your trip"
    return score, why


def _score_interest(row: dict, ctx: MatchContext) -> tuple[float, str | None]:
    wanted = ctx.interest_tokens
    if not wanted:
        return NEUTRAL, None

    have = _tokens(row.get("tags"), row.get("title"), row.get("summary"), row.get("amenities"))
    overlap = wanted & have
    if not overlap:
        return 0.25, None

    score = _clamp(len(overlap) / max(1, min(len(ctx.interests) or 1, 3)))
    named = next((i for i in ctx.interests if _norm_place(i) in {_norm_place(o) for o in overlap}), None)
    why = f"Matches your interest in {named.lower()}" if named else None
    return score, why


def _score_duration(row: dict, ctx: MatchContext) -> tuple[float, str | None]:
    pkg_days = row.get("duration_days")
    if not pkg_days:
        return NEUTRAL, None
    delta = abs(int(pkg_days) - ctx.days)
    if delta == 0:
        return 1.0, f"Covers all {ctx.days} days of your trip"
    if delta == 1:
        return 0.7, f"{pkg_days} days against your {ctx.days}"
    if delta == 2:
        return 0.4, None
    return 0.1, None


def _score_date(row: dict, ctx: MatchContext) -> tuple[float, str | None]:
    raw = row.get("departure_date")
    if not raw or not ctx.depart_date:
        return NEUTRAL, None
    try:
        dep = datetime.fromisoformat(str(raw)).date()
    except ValueError:
        return NEUTRAL, None

    delta = abs((dep - ctx.depart_date).days)
    if delta <= 3:
        return 1.0, f"Departs {dep.strftime('%-d %b')}, right around your dates"
    if delta <= 10:
        return 0.7, f"Departs {dep.strftime('%-d %b')}"
    if delta <= 30:
        return 0.4, None
    return 0.1, None


def _score_completeness(row: dict) -> tuple[float, str | None]:
    photos = row.get("photos") or ([row["hero_image"]] if row.get("hero_image") else [])
    content = row.get("content") or {}
    score = 0.0
    if len(photos) >= 3:
        score += 0.30
    elif photos:
        score += 0.15
    if len((row.get("description") or "")) >= 120:
        score += 0.20
    if row.get("price_from"):
        score += 0.20
    if row.get("summary"):
        score += 0.15

    inclusions = content.get("inclusions") or content.get("rooms") or []
    why = None
    if inclusions:
        score += 0.15
        if content.get("inclusions"):
            why = f"{len(content['inclusions'])} things included"
    return _clamp(score), why


def _score_trust(row: dict) -> tuple[float, str | None]:
    partner = row.get("partner") or {}
    if not partner:
        # A TravBuddy-run trip. Attribution travels in `partner_name`, not in
        # `why` — "who runs it" is not a reason to book it.
        return 0.9, None

    score = 0.6 if partner.get("verified") else 0.25
    rating = partner.get("rating")
    why = None
    if rating:
        score += 0.4 * _clamp(float(rating) / 5.0)
        if float(rating) >= 4.5 and partner.get("review_count"):
            why = f"Rated {rating} by {partner['review_count']} travellers"
    if not why and partner.get("verified"):
        why = f"Verified partner — {partner.get('brand_name')}"

    minutes = partner.get("response_minutes")
    if minutes and minutes <= 120:
        score = _clamp(score + 0.05)
    return _clamp(score), why


def _score_freshness(row: dict) -> tuple[float, str | None]:
    raw = row.get("updated_at")
    if not raw:
        return NEUTRAL, None
    try:
        updated = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return NEUTRAL, None
    age = (datetime.now(timezone.utc) - updated).days
    if age <= 30:
        return 1.0, None
    if age <= 90:
        return 0.7, None
    return 0.4, None


# ── day attachment ───────────────────────────────────────────────────────────

def _match_day(row: dict, ctx: MatchContext) -> tuple[int, str] | None:
    """Pin an experience to the itinerary day it genuinely belongs to.

    Matches on place words shared between the listing and the day's planned
    activities — "Solang Valley Paragliding" against a day that visits Solang.
    Generic travel words are excluded so 'day'/'valley' alone can't match.
    """
    if not ctx.day_plan:
        return None

    STOP = {"the", "and", "a", "of", "to", "in", "at", "day", "trip", "tour",
            "visit", "valley", "local", "sightseeing", "free", "morning",
            "afternoon", "evening", "night", "explore", "time"}

    # The trip's own destination is noise here: EVERY day of a Manali trip is
    # in Manali, so leaving "manali" in would tie every day at 1 and pin the
    # experience to Day 1 by accident. Only the distinguishing words count.
    noise = STOP | _tokens(ctx.destination, ctx.origin)

    mine = _tokens(row.get("title"), row.get("city"), row.get("destination"), row.get("tags")) - noise
    if not mine:
        return None

    best: tuple[int, str] | None = None
    best_n = 0
    for day in ctx.day_plan:
        overlap = mine & (day.tokens - noise)
        if len(overlap) > best_n:
            best_n = len(overlap)
            place = day.title or ", ".join(day.places[:1])
            best = (day.day, f"Fits your Day {day.day} plans{f' in {place}' if place else ''}")
    return best if best_n >= 1 else None


# ── item assembly ────────────────────────────────────────────────────────────

def _cta(booking_mode: str | None) -> tuple[str, str]:
    """(booking_type, cta_label)."""
    mode = booking_mode or "whatsapp"
    if mode == "instant":
        return ("instant", "Book now") if BOOKINGS_LIVE else ("instant", "Check availability")
    if mode == "enquiry":
        return "enquiry", "Request a quote"
    return "whatsapp", "Chat on WhatsApp"


def _build_item(row: dict, kind: str, score: float, whys: list[str], day: int | None) -> dict:
    partner = row.get("partner") or {}
    booking_type, cta = _cta(row.get("booking_mode"))

    if kind == "package":
        url = f"/trips/{row['slug']}"
        image = row.get("hero_image")
        price_unit = "per_person"
    else:
        url = f"/{'stays' if kind == 'hotel' else 'experiences'}/{row['slug']}"
        photos = row.get("photos") or []
        image = photos[0] if photos else None
        price_unit = row.get("price_unit") or ("per_night" if kind == "hotel" else "per_person")

    return {
        "id": row["id"],
        "kind": kind,
        "slug": row["slug"],
        "title": row["title"],
        "summary": row.get("summary") or row.get("description") or None,
        "image": image,
        "price": row.get("price_from"),
        "price_unit": price_unit,
        "url": url,
        "booking_type": booking_type,
        "cta": cta,
        "partner_name": partner.get("brand_name") if partner else "TravBuddy",
        "partner_slug": partner.get("slug") if partner else None,
        "verified": bool(partner.get("verified")) if partner else True,
        "confidence": round(score, 3),
        "why": whys[:3],
        "day": day,
    }


# Only three `why` lines survive to the card, so they are chosen by how much
# they help a traveller decide — not by the order the scorers happen to run in.
# Without this, a package's trust line was being truncated away by its own
# budget/duration/destination lines.
WHY_PRIORITY = [
    "budget",       # "fits your ₹15,000"
    "duration",     # "covers all 4 days"
    "trust",        # "rated 4.8 by 310 travellers"
    "destination",  # "in Manali, exactly where you're headed"
    "interest",     # "matches your interest in adventure"
    "completeness", # "8 things included"
    "date",
]


def _renderable(row: dict, kind: str) -> bool:
    """Would this produce a card worth showing?

    A recommendation with no photo or no price renders as a broken tile, and a
    broken tile is worse than an empty section. Published listings clear this
    via the publish rules already — this is defence in depth for legacy rows
    and for anything that slipped through.
    """
    has_image = bool(row.get("hero_image")) or bool(row.get("photos"))
    return has_image and bool(row.get("price_from"))


def _score_row(row: dict, ctx: MatchContext, kind: str) -> tuple[float, list[str]] | None:
    """None means "drop it" — a hard filter fired."""
    dest_score, dest_why = _score_destination(row, ctx)
    if dest_score <= 0.0:
        return None  # wrong place; never show it

    budget_score, budget_why = _score_budget(row, ctx, kind)
    if budget_score < 0:
        return None  # over budget; never show it

    if not _renderable(row, kind):
        return None  # nothing to show; an empty section beats a broken card

    parts: dict[str, tuple[float, str | None]] = {
        "destination": (dest_score, dest_why),
        "budget": (budget_score, budget_why),
        "interest": _score_interest(row, ctx),
        "duration": _score_duration(row, ctx) if kind == "package" else (NEUTRAL, None),
        "date": _score_date(row, ctx) if kind == "package" else (NEUTRAL, None),
        "completeness": _score_completeness(row),
        "trust": _score_trust(row),
        "freshness": _score_freshness(row),
    }

    total = sum(WEIGHTS[name] * value for name, (value, _) in parts.items())
    whys = [parts[name][1] for name in WHY_PRIORITY if parts.get(name, (0, None))[1]]
    return total, whys


# ── public entry point ───────────────────────────────────────────────────────

def match_inventory(ctx: MatchContext) -> dict:
    """
    Rank marketplace inventory against a trip intent.

    Returns a generic envelope — `sections[]` of `kind`-tagged items plus
    `day_matches[]`. The planner renders these without knowing what any
    particular kind is, so new inventory types need no frontend change.
    """
    started = time.perf_counter()

    try:
        listings, trips = _fetch_inventory(ctx.destination)
    except Exception as exc:  # noqa: BLE001 — a marketplace miss must never break planning
        logger.warning("[marketplace-match] inventory fetch failed: %s", type(exc).__name__)
        return _empty(started, error="inventory_unavailable")

    scored: dict[str, list[dict]] = {"package": [], "hotel": [], "experience": []}

    for row in trips:
        result = _score_row(row, ctx, "package")
        if not result:
            continue
        score, whys = result
        if score >= MIN_CONFIDENCE:
            seats = row.get("seats_left")
            if seats is not None and seats <= 0:
                continue
            if seats is not None and seats <= 6:
                whys.append(f"Only {seats} seats left")
            scored["package"].append(_build_item(row, "package", score, whys, None))

    for row in listings:
        kind = "hotel" if row.get("kind") == "hotel" else "experience"
        result = _score_row(row, ctx, kind)
        if not result:
            continue
        score, whys = result

        day = None
        if kind == "experience":
            hit = _match_day(row, ctx)
            if hit:
                day, day_why = hit
                whys.insert(0, day_why)
                score = _clamp(score + 0.05)  # relevance to the actual plan, not a paid boost

        if score >= MIN_CONFIDENCE:
            scored[kind].append(_build_item(row, kind, score, whys, day))

    for items in scored.values():
        items.sort(key=lambda i: i["confidence"], reverse=True)

    packages = scored["package"][:MAX_PACKAGES]
    hotels = scored["hotel"][:MAX_HOTELS]
    experiences = scored["experience"][:MAX_EXPERIENCES]

    sections = []
    if packages:
        sections.append({"kind": "package", "title": "A ready-made trip", "items": packages})
    if hotels:
        sections.append({"kind": "hotel", "title": "Where to stay", "items": hotels})
    if experiences:
        sections.append({"kind": "experience", "title": "Things to do", "items": experiences})

    # Only offer to replace the whole itinerary when the package is genuinely
    # strong AND actually spans the trip. Anything less would be a sales pitch.
    replacement = None
    if packages and packages[0]["confidence"] >= REPLACEMENT_CONFIDENCE:
        top = packages[0]
        if any("Covers all" in w for w in top["why"]):
            replacement = top

    day_matches = [e for e in experiences if e["day"] is not None]

    return {
        "sections": sections,
        "day_matches": day_matches,
        "package_replacement": replacement,
        "meta": {
            "destination": ctx.destination,
            "candidates": len(listings) + len(trips),
            "returned": len(packages) + len(hotels) + len(experiences),
            "min_confidence": MIN_CONFIDENCE,
            "took_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    }


def _empty(started: float, error: str | None = None) -> dict:
    return {
        "sections": [],
        "day_matches": [],
        "package_replacement": None,
        "meta": {
            "candidates": 0,
            "returned": 0,
            "took_ms": round((time.perf_counter() - started) * 1000, 1),
            **({"error": error} if error else {}),
        },
    }


def clear_cache() -> None:
    """Test hook — the inventory memo is process-local."""
    with _cache_lock:
        _cache.clear()
