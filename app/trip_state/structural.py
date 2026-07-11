"""
Structural copilot edits — the "travel agent" moves that change the SHAPE of a
trip, not just a pick within it:

  handle_add_activities — add real, Google-Places-verified spots to a day (or a
                          fresh block of spots) on demand. No hallucination: every
                          added spot is a live Places result with a place_id.
  handle_add_waypoint   — add a stopover city "in between" origin and
                          destination (e.g. Nainital on a Bhiwani→Meghalaya
                          trip). Re-plans the going journey as two real segments
                          (origin→waypoint, waypoint→destination), generates a
                          short itinerary for the waypoint, splices it in, and
                          reprices the trip.

Both reuse the SAME production pipelines the initial plan uses
(discover_places / search_places, run_plan_journey, run_plan_trip) so the data
is identical in shape and quality to a freshly-planned trip.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from app.services.places import discover_places, search_places

from .patch import PatchOp

logger = logging.getLogger(__name__)

# Google Places `types` → the app's activity category + coarse type. First match
# wins, so order matters (specific before generic).
_TYPE_MAP: list[tuple[str, str, str]] = [
    ("cafe", "Cafe", "food"),
    ("bakery", "Cafe", "food"),
    ("bar", "Bar", "nightlife"),
    ("night_club", "Bar", "nightlife"),
    ("restaurant", "Restaurant", "food"),
    ("meal_takeaway", "Street Food", "food"),
    ("museum", "Museum", "attraction"),
    ("art_gallery", "Gallery", "attraction"),
    ("hindu_temple", "Temple", "attraction"),
    ("place_of_worship", "Temple", "attraction"),
    ("park", "Park", "attraction"),
    ("natural_feature", "Viewpoint", "experience"),
    ("campground", "Camping", "experience"),
    ("tourist_attraction", "Landmark", "attraction"),
    ("point_of_interest", "Landmark", "attraction"),
]


def _norm(title: str | None) -> str:
    return (title or "").strip().lower()


def _category_and_type(place: dict) -> tuple[str, str]:
    types = [t.lower() for t in (place.get("types") or [])]
    for key, cat, typ in _TYPE_MAP:
        if key in types:
            return cat, typ
    return "Landmark", "attraction"


def _place_to_activity(place: dict, destination: str) -> dict:
    """Turn a Google Places search result into an itinerary activity, matching
    the shape the generation pipeline produces (place_id, coords, rating drive
    the confidence score; start_time/duration are filled by the caller)."""
    geo = (place.get("geometry") or {}).get("location") or {}
    photo_ref = place.get("photo_reference")
    cat, typ = _category_and_type(place)
    place_id = place.get("place_id")
    return {
        "title": place.get("name"),
        "category": cat,
        "type": typ,
        "source": "google_places",
        "place_id": place_id,
        "best_time": "Anytime",
        "lat": geo.get("lat"),
        "lng": geo.get("lng"),
        "rating": place.get("rating"),
        "user_ratings_total": place.get("user_ratings_total"),
        "address": place.get("formatted_address"),
        "photo_reference": photo_ref,
        "photo_url": f"/api/v1/images/{photo_ref}" if photo_ref else None,
        "maps_url": (
            f"https://www.google.com/maps/place/?q=place_id:{place_id}"
            if place_id else None
        ),
        "description": f"A well-rated spot in {destination}, added on your request.",
        "tips": None,
        "must_try": None,
        "added_by": "copilot",
    }


def _retime_day(day: dict, budget: str) -> dict:
    """Fill deterministic fields + re-order/re-time a single day's activities."""
    from app.api.v1.plan_trip.helpers import (
        assign_activity_times,
        fill_deterministic_fields,
        nearest_neighbor_sort,
    )
    from app.api.v1.plan_trip.validation import annotate_confidence

    acts = day.get("activities") or []
    fill_deterministic_fields([day], "₹", budget)
    if len(acts) >= 2:
        day["activities"] = nearest_neighbor_sort(acts)
    assign_activity_times([day])
    annotate_confidence([day])
    return day


# ── Add activities ──────────────────────────────────────────────────────────────

async def handle_add_activities(
    state: dict, day_index: int | None, query: str, count: int | None,
) -> tuple[list[PatchOp], str]:
    """Add live, verified spots to the itinerary. `day_index` targets a specific
    day (0-based); None appends to the last day. `query` narrows the kind of spot
    ('waterfalls', 'cafes', 'adventure'); empty means 'more of the good stuff'."""
    req = state.get("request") or {}
    destination = req.get("destination")
    days = list((state.get("itinerary") or {}).get("days") or [])
    if not destination:
        return [], "I don't know this trip's destination yet, so I can't add spots."
    if not days:
        return [], "There's no itinerary to add to yet — let the plan finish first."

    budget = req.get("budget_tier") or "mid"
    want = max(1, min(int(count or 3), 6))

    query = (query or "").strip()
    if query:
        results = await search_places(f"{query} in {destination}")
    else:
        disc = await discover_places(destination)
        # Interleave categories so we don't dump 5 cafes in a row.
        buckets = [lst for lst in disc.values() if lst]
        results = [p for i in range(max((len(b) for b in buckets), default=0))
                   for b in buckets if i < len(b)]

    existing = {
        _norm(a.get("title"))
        for d in days for a in (d.get("activities") or [])
    }
    new_acts: list[dict] = []
    for p in results:
        title = p.get("name")
        if not title or _norm(title) in existing:
            continue
        if not p.get("place_id"):
            continue
        new_acts.append(_place_to_activity(p, destination))
        existing.add(_norm(title))
        if len(new_acts) >= want:
            break

    if not new_acts:
        what = f"“{query}” spots" if query else "new spots"
        return [], (
            f"I looked but couldn't find fresh {what} in {destination} that aren't "
            "already on your plan. Try naming an area or a different kind of place."
        )

    target = day_index if (day_index is not None and 0 <= day_index < len(days)) else len(days) - 1
    day = dict(days[target])
    day["activities"] = list(day.get("activities") or []) + new_acts
    day = _retime_day(day, budget)
    days[target] = day

    patches = [PatchOp(op="set", path=f"/itinerary/days/{target}", value=day)]
    names = ", ".join(a["title"] for a in new_acts)
    where = f"Day {target + 1}" + (f" ({day.get('theme') or day.get('title')})" if day.get("theme") or day.get("title") else "")
    reply = (
        f"Added {len(new_acts)} spot{'s' if len(new_acts) > 1 else ''} to {where}: "
        f"{names}. I re-timed the day so it still flows."
    )
    return patches, reply


# ── Add a waypoint / stopover city ───────────────────────────────────────────────

def _renumber(days: list[dict]) -> list[dict]:
    for i, d in enumerate(days):
        d["day"] = i + 1
    return days


def _plus_days(date_str: str | None, n: int) -> str | None:
    if not date_str:
        return None
    try:
        return (datetime.strptime(date_str[:10], "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")
    except ValueError:
        return None


async def handle_add_waypoint(
    state: dict, user_id: str, city: str, waypoint_days: int | None,
) -> tuple[list[PatchOp], str]:
    """Add a stopover city between origin and destination: re-route the going
    journey through it, build a short itinerary for it, splice it in, reprice."""
    from app.api.v1.plan_trip.journey import run_plan_journey
    from app.api.v1.plan_trip.models import PlanJourneyRequest, PlanTripRequest
    from app.api.v1.plan_trip.orchestrator import run_plan_trip
    from app.api.v1.plan_trip.plan import _shape_journey

    from . import copilot

    req = state.get("request") or {}
    origin = req.get("origin")
    destination = req.get("destination")
    city = (city or "").strip()
    if not city:
        return [], "Which city would you like to stop in?"
    if not origin or not destination:
        return [], "I need this trip's origin and destination set before adding a stopover."

    wp_days = max(1, min(int(waypoint_days or 1), 4))
    travelers = max(int(req.get("travelers") or 1), 1)
    budget = req.get("budget_tier") or "mid"
    dep = req.get("departure_date")
    onward_dep = _plus_days(dep, wp_days)

    seg1_req = PlanJourneyRequest(
        origin=origin, destination=city, travelers=travelers,
        budget=budget, departure_date=dep, stay_nights=wp_days,
    )
    seg2_req = PlanJourneyRequest(
        origin=city, destination=destination, travelers=travelers,
        budget=budget, departure_date=onward_dep, stay_nights=max(int(req.get("stay_nights") or 1), 1),
    )
    wp_trip_req = PlanTripRequest(
        destination=city, days=wp_days, budget=budget, travelers=travelers,
        start_date=dep, end_date=onward_dep, skip_user_trips=True,
    )

    # All three run against the same live pipelines the initial plan uses.
    # profile={} skips the per-user profile DB read; each guards its own errors.
    seg1, seg2, wp_trip = await asyncio.gather(
        run_plan_journey(seg1_req, user_id, profile={}),
        run_plan_journey(seg2_req, user_id, profile={}),
        run_plan_trip(wp_trip_req, user_id, profile={}),
        return_exceptions=True,
    )

    def _ok(x) -> dict | None:
        return x if isinstance(x, dict) else None

    seg1, seg2, wp_trip = _ok(seg1), _ok(seg2), _ok(wp_trip)

    patches: list[PatchOp] = []

    # ── Itinerary: splice the waypoint's days in BEFORE the destination days ──────
    wp_days_list = _renumber([dict(d) for d in ((wp_trip or {}).get("days") or [])])
    for d in wp_days_list:
        d["waypoint"] = city
    existing_days = list((state.get("itinerary") or {}).get("days") or [])
    if wp_days_list:
        merged_days = _renumber(wp_days_list + existing_days)
        state.setdefault("itinerary", {})["days"] = merged_days
        patches.append(PatchOp(op="set", path="/itinerary/days", value=merged_days))

    # ── Transport: origin→city becomes the going journey; city→destination is
    #    an explicit onward segment. Both carry their own live options + pick. ──────
    routed = False
    if seg1:
        going = _shape_journey(seg1, include_hotels=False)
        state.setdefault("transport", {})["going"] = going
        patches.append(PatchOp(op="set", path="/transport/going", value=going))
        routed = True
    if seg2:
        onward = _shape_journey(seg2, include_hotels=False)
        onward["from"] = city
        onward["to"] = destination
        state.setdefault("transport", {})["onward_segments"] = [onward]
        patches.append(PatchOp(op="set", path="/transport/onward_segments", value=[onward]))
        routed = True

    # ── Request: record the stopover and grow the trip length ────────────────────
    new_req = dict(req)
    new_req["waypoints"] = list(req.get("waypoints") or []) + [city]
    new_req["days"] = int(req.get("days") or len(existing_days) or 1) + len(wp_days_list)
    new_req["stay_nights"] = int(req.get("stay_nights") or 1) + wp_days
    state["request"] = new_req
    patches.append(PatchOp(op="set", path="/request", value=new_req))

    # ── Reprice (transport recompute now folds in the onward segment) ────────────
    if routed:
        new_costs = copilot._recompute_costs(state)
        state["costs"] = new_costs
        patches.append(PatchOp(op="set", path="/costs", value=new_costs))

    # ── Reply ────────────────────────────────────────────────────────────────────
    spot_names = [
        a.get("title")
        for d in wp_days_list for a in (d.get("activities") or [])
    ][:3]
    bits = []
    if wp_days_list:
        bits.append(
            f"added {len(wp_days_list)} day{'s' if len(wp_days_list) > 1 else ''} in {city}"
            + (f" ({', '.join(n for n in spot_names if n)})" if spot_names else "")
        )
    if routed:
        bits.append(f"re-routed the trip {origin} → {city} → {destination}")
    if not bits:
        return [], (
            f"I couldn't pull live routes or spots for {city} right now — "
            "give it another try in a moment."
        )
    live = copilot._live_total(state)
    tail = f" Rides + stay are now ₹{live:,} at live prices." if live else ""
    return patches, f"Done — {' and '.join(bits)}.{tail}"
