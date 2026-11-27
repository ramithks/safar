"""
TripGraph → TripDoc serialization + the doc-level budget math.

The doc is render-ready: options are inlined, TRIMMED (display + booking
fields only, capped, never provider `raw`), pre-ranked, and the recommendation
is PERSISTED as `recommended_id` — recomputed only when generation or a
budget re-rank runs, which is exactly when it can change.

Only the ACTIVE route of each leg is serialized (the selected one, else the
top-ranked). Route alternatives existed for the copilot's "switch to the
gateway route" move, which is gone; a regeneration rebuilds them anyway.

Everything here is pure (graph/doc in → doc out); persistence lives in store.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.trip_state import graph as G

# Caps keep the doc a fast single-row read (~100-400 KB).
MAX_OPTIONS_PER_HOP = 12
MAX_HOTELS_PER_STOP = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return None


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# ── option / hotel trimming ──────────────────────────────────────────────────

# Provider display fields the UI renders on ride cards (train classes + seat
# predictions, flight fare breakdown, bus boarding points, …). Whitelisted
# straight through from `raw` — small scalars/lists, never the whole payload.
_OPTION_DETAIL_FIELDS = (
    # common
    "source", "provider", "rating", "is_refundable",
    # recommendation-agent notes ("why we'd pick this")
    "ai_reason", "ai_pros", "ai_best_for", "recommendation_reason",
    # train
    "train_number", "train_name", "available_classes", "cheapest_class",
    "cheapest_fare", "from_station_name", "from_station_code",
    "to_station_name", "to_station_code", "distance_km", "pantry",
    # flight
    "airline", "flight_number", "airline_logo", "cabin_class",
    "departure_terminal", "from_airport", "to_airport",
    "price_base_fare", "price_tax", "price_total", "price",
    "baggage_kg", "stops",
    # bus
    "bus_operator", "bus_type", "boarding_point", "dropping_point",
    "available_seats", "total_seats", "fare_discounted", "fare_original",
    # times as the providers shaped them (the UI clock-parses either)
    "departure_time", "arrival_time", "duration",
)


def trim_transport_option(opt: G.TransportOption) -> dict:
    """Display + booking subset of one ride: the normalized core the doc code
    reads (id/mode/fare_per_person/…) plus the whitelisted provider display
    fields the ride cards render. Never the whole `raw`."""
    raw = opt.raw or {}
    out = {k: raw.get(k) for k in _OPTION_DETAIL_FIELDS if raw.get(k) is not None}
    out.update(_clean({
        "id": opt.id,
        "mode": opt.mode,
        "name": G._name(opt.mode, raw) if raw else (opt.operator or opt.mode),
        "operator": opt.operator,
        "service_no": _first(raw, "flight_number", "train_number", "service_number"),
        "departure": opt.departure,
        "arrival": opt.arrival,
        "duration_min": opt.duration_min if opt.duration_min is not None else raw.get("duration_min"),
        "fare_per_person": opt.fare_per_person,
        "currency": opt.currency or "INR",
        "available": opt.available if opt.available is not None else (G._sold_out(opt) == 0),
        "booking_url": _first(raw, "booking_url", "booking_link", "deep_link", "url"),
    }))
    return out


def hotel_id_of(h: dict) -> str:
    """Stable identity for selection — same rule stay_price_for_stop used, so
    legacy selected ids keep matching after conversion."""
    return str(h.get("id") or h.get("hotel_id") or h.get("name") or "")


def _hotel_photo(h: dict):
    photos = h.get("photos") or h.get("images") or []
    if isinstance(photos, list) and photos:
        p = photos[0]
        if isinstance(p, str):
            return p
        if isinstance(p, dict):
            return _first(p, "url", "src", "large", "medium")
    return _first(h, "photo", "image", "thumbnail", "cover_photo")


# Hotel card fields kept under their ORIGINAL provider names so the existing
# HotelCard / map renderers work unchanged. Heavy blobs (full photo galleries,
# landmark lists, pricing stories, raw) are dropped or capped.
_HOTEL_FIELDS = (
    "name", "hotel_id", "source", "provider", "currency",
    "price_per_night", "price", "price_total", "total_price",
    "star_rating", "review_score", "review_label", "review_count",
    "unified_rating", "unified_rating_label", "unified_rating_basis",
    "address", "area", "locality", "property_type",
    "distance_km", "distance_from_center_km", "distance_from_search_km",
    "search_landmark", "booking_url",
    "free_cancellation", "no_prepayment", "breakfast_included",
    # coordinates under every provider spelling — the stays map plots these
    "latitude", "longitude", "lat", "lng", "lon", "long",
    # recommendation-agent notes ("why we'd pick this")
    "ai_reason", "ai_pros", "ai_best_for", "recommendation_reason",
    "why_recommended", "reason", "why", "is_recommended",
    # Safar partner stays — the card renders the Partner tag off these and
    # links to /stays/{listing_slug} instead of an OTA. Dropping any of them
    # turns a bookable partner row into an anonymous hotel.
    "is_partner", "partner_name", "partner_slug", "partner_verified",
    "listing_id", "listing_slug", "booking_mode", "available_rooms", "city",
    # strike-through pricing — emitted by the Agoda/Booking parsers and by
    # partner stays alike, so the card shows "₹3,600 ₹3,900 · 8% off"
    "price_original", "savings", "discount_pct", "discount_label",
)
_MAX_HOTEL_PHOTOS = 4
_MAX_HOTEL_AMENITIES = 8


def trim_hotel(h: dict) -> dict:
    """Card subset of one stay option — original field names, capped lists,
    coordinates kept (the stays map plots them), no raw."""
    out = {k: h.get(k) for k in _HOTEL_FIELDS if h.get(k) is not None}
    out["id"] = hotel_id_of(h)
    # Nested coords shapes survive so the map's coords() helper finds them.
    for key in ("location", "geo", "coordinates", "coords"):
        v = h.get(key)
        if isinstance(v, dict):
            out[key] = v
            break
    photos = h.get("photos") or h.get("images") or []
    if isinstance(photos, list) and photos:
        out["photos"] = photos[:_MAX_HOTEL_PHOTOS]
    photo = _hotel_photo(h)
    if photo:
        out["photo"] = photo
    amenities = h.get("amenities")
    if isinstance(amenities, list) and amenities:
        out["amenities"] = amenities[:_MAX_HOTEL_AMENITIES]
    return out


# ── graph → doc sections ────────────────────────────────────────────────

def _return_deadline(spec: dict):
    """(arrive_by, not_before) on the trip's last day: the end clock binds the
    arrival home; the traveller's own start clock (default 06:00) is the
    earliest a same-day return can leave and still be day travel. None when
    no end_time was requested."""
    dep, end_time = spec.get("departure_date"), spec.get("end_time")
    if not dep or not end_time:
        return None
    try:
        from datetime import date as _date, datetime, timedelta
        y, m, d = (int(x) for x in str(dep).split("-"))
        days = max(int(spec.get("days") or 1), 1)
        last = _date(y, m, d) + timedelta(days=days - 1)
        hh, mm = int(end_time[:2]), int(end_time[3:5])
        st = spec.get("start_time") or "06:00"
        sh, sm = int(st[:2]), int(st[3:5])
        base = datetime(last.year, last.month, last.day)
        return (base.replace(hour=hh, minute=mm), base.replace(hour=sh, minute=sm))
    except Exception:  # noqa: BLE001
        return None


# ─────

def _leg_entry(leg: G.TripLeg, budget: str | None, searched_at: str,
               arrive_by=None, not_before=None) -> dict:
    route = G.active_route(leg, None, budget, arrive_by=arrive_by, not_before=not_before)
    hops: list[dict] = []
    if route:
        sel = leg.selection if (leg.selection and leg.selection.route_id == route.id) else None
        for i, hop in enumerate(route.hops):
            # The end-clock window binds the hop that lands home.
            final = i == len(route.hops) - 1
            ranked = G.rank_options(hop.options, None, budget,
                                    arrive_by=arrive_by if final else None,
                                    not_before=not_before if final else None,
                                    hop_date=hop.date or leg.date)[:MAX_OPTIONS_PER_HOP]
            options = [trim_transport_option(o) for o in ranked]
            selected_id = None
            if sel and i < len(sel.per_hop_option_ids):
                oid = sel.per_hop_option_ids[i]
                if oid and any(o["id"] == oid for o in options):
                    selected_id = oid
            hops.append(_clean({
                "from": hop.from_city,
                "to": hop.to_city,
                "date": hop.date or leg.date,
                "options": options,
                "selected_id": selected_id,
                "recommended_id": options[0]["id"] if options else None,
            }))
    return _clean({
        "leg_id": leg.id,
        "from": leg.from_city,
        "to": leg.to_city,
        "date": leg.date,
        "status": "ok" if (route and any(h.get("options") for h in hops)) else "no_route",
        "via": route.gateway_city if (route and route.kind == "via_gateway") else None,
        "hops": hops,
        "searched_at": searched_at,
    })


def _stop_entry(node: G.TripStop, searched_at: str) -> dict:
    hotels = [trim_hotel(h) for h in (node.stay.search_results or [])[:MAX_HOTELS_PER_STOP]
              if isinstance(h, dict)]
    selected_id = node.stay.selected_id
    if selected_id and not any(h.get("id") == selected_id for h in hotels):
        selected_id = None
    # The recommendation-agent's pick (is_recommended) wins; else cheapest.
    recommended = next((h for h in hotels if h.get("is_recommended")), None) \
        or (min(hotels, key=lambda h: h.get("price_per_night") or 1e12) if hotels else None)
    return _clean({
        "stop_id": node.id,
        "city": node.city,
        "role": node.role,
        "nights": node.nights or 0,
        "arrival_date": node.arrival_date,
        "departure_date": node.departure_date,
        "coordinates": node.coordinates,
        "days": node.days or [],
        "hotels": {
            "options": hotels,
            "selected_id": selected_id,
            "recommended_id": recommended.get("id") if recommended else None,
            "searched_at": searched_at,
        },
    })


def doc_from_graph(
    trip_id: str,
    spec: dict,
    graph: G.TripGraph,
    *,
    title: str | None = None,
    status: str = "ready",
    progress: int = 100,
    destination: dict | None = None,
    weather: dict | None = None,
    documents: dict | None = None,
    notes: list[str] | None = None,
    generation: dict | None = None,
    base_budget: dict | None = None,
) -> dict:
    """Serialize an in-memory graph (mid- or post-generation) into the doc."""
    budget_tier = spec.get("budget_tier") or spec.get("budget") or "mid"
    searched_at = _now()
    ordered_nodes, outbound, inbound = G.ordered_path(graph)
    legs = outbound + ([inbound] if inbound else [])
    window = _return_deadline(spec)
    arrive_by, not_before = window if window else (None, None)

    doc = {
        "id": trip_id,
        "title": title or f"{spec.get('origin')} → {spec.get('destination')}",
        "status": status,
        "progress": progress,
        "spec": dict(spec),
        "route": [n.city for n in ordered_nodes] + ([ordered_nodes[0].city] if inbound and ordered_nodes else []),
        "destination": destination or {"name": spec.get("destination")},
        "itinerary": [_stop_entry(n, searched_at) for n in ordered_nodes if n.role != "origin"],
        "transport": [_leg_entry(leg, budget_tier, searched_at,
                                 arrive_by=(arrive_by if inbound is not None and leg is inbound else None),
                                 not_before=(not_before if inbound is not None and leg is inbound else None))
                      for leg in legs],
        "weather": weather or {},
        "documents": documents or {},
        "notes": notes or [],
        "generation": generation or {},
        "generated_at": searched_at,
        "updated_at": searched_at,
    }
    doc["budget"] = compute_budget(doc, base=base_budget)
    return doc


# ── budget: the ONE cost computation, doc in → budget out ────────────────────

def _chosen_option(hop: dict) -> dict | None:
    options = hop.get("options") or []
    if not options:
        return None
    for key in ("selected_id", "recommended_id"):
        oid = hop.get(key)
        if oid:
            match = next((o for o in options if o.get("id") == oid), None)
            if match:
                return match
    return options[0]


def chosen_hotel(stop: dict) -> dict | None:
    block = stop.get("hotels") or {}
    options = block.get("options") or []
    if not options:
        return None
    oid = block.get("selected_id")
    if oid:
        match = next((h for h in options if h.get("id") == oid), None)
        if match:
            return match
    return min(options, key=lambda h: h.get("price_per_night") or 1e12)


def compute_budget(doc: dict, base: dict | None = None) -> dict:
    """Rebuild the budget block from the doc — same semantics graph_costs had:
    transport = active option per hop × travelers; stay = chosen hotel × nights;
    misc = 10%. food/activities carry over from `base` when present."""
    spec = doc.get("spec") or {}
    travelers = max(int(spec.get("travelers") or 1), 1)
    breakdown = dict((base or doc.get("budget") or {}).get("breakdown") or {})

    per_person = 0.0
    for leg in doc.get("transport") or []:
        for hop in leg.get("hops") or []:
            opt = _chosen_option(hop)
            fare = (opt or {}).get("fare_per_person")
            per_person += fare if isinstance(fare, (int, float)) else 0

    stay_total = 0.0
    for stop in doc.get("itinerary") or []:
        hotel = chosen_hotel(stop)
        price = (hotel or {}).get("price_per_night")
        nights = max(int(stop.get("nights") or 0), 0)
        stay_total += (price if isinstance(price, (int, float)) else 0) * nights

    breakdown["transport"] = round(per_person * travelers)
    breakdown["stay"] = round(stay_total)
    breakdown["misc"] = round((breakdown["transport"] + breakdown["stay"]) * 0.10)
    total = sum(breakdown.get(k, 0) for k in ("transport", "stay", "food", "activities", "misc"))
    return {
        "breakdown": breakdown,
        "total": total,
        "per_person": total // travelers,
        "currency": (base or doc.get("budget") or {}).get("currency") or "INR",
    }


# ── budget re-rank (change_budget action: no re-search, no LLM) ──────────────

_AXIS = {"low": "cheapest", "mid": "balanced", "high": "fastest"}


def rerank_for_budget(doc: dict, budget_tier: str) -> dict:
    """Re-order every hop's options for the new tier and re-stamp
    recommended_id. Selections are untouched; budget recomputed."""
    axis = _AXIS.get(budget_tier, "balanced")

    def key(o: dict):
        fare = o.get("fare_per_person") or 1e12
        dur = o.get("duration_min") or 1e12
        sold_out = 0 if o.get("available", True) else 1
        if axis == "cheapest":
            return (sold_out, fare, dur)
        if axis == "fastest":
            return (sold_out, dur, fare)
        return (sold_out, 0.6 * fare + 0.4 * dur * 5)  # balanced (~₹5/min)

    for leg in doc.get("transport") or []:
        for hop in leg.get("hops") or []:
            options = sorted(hop.get("options") or [], key=key)
            hop["options"] = options
            hop["recommended_id"] = options[0]["id"] if options else None

    doc.setdefault("spec", {})["budget_tier"] = budget_tier
    doc["budget"] = compute_budget(doc)
    doc["updated_at"] = _now()
    return doc


# ── selection carry-forward across regenerations ─────────────────────────────

def _option_identity(o: dict) -> tuple:
    dep = str(o.get("departure") or "")
    return (
        o.get("mode"),
        (o.get("service_no") or o.get("name") or o.get("operator") or "").strip().lower() or None,
        dep[11:16] if len(dep) >= 16 else dep[:5],
    )


def carry_forward_selections(prior_doc: dict | None, doc: dict) -> list[str]:
    """Re-apply the user's picks from the previous doc wherever the same ride /
    hotel still exists in the regenerated one. Returns notes for picks that
    couldn't be kept."""
    if not prior_doc:
        return []
    dropped: list[str] = []

    prior_legs = {leg.get("leg_id"): leg for leg in prior_doc.get("transport") or []}
    for leg in doc.get("transport") or []:
        prior = prior_legs.get(leg.get("leg_id"))
        if not prior:
            continue
        for hop, prior_hop in zip(leg.get("hops") or [], prior.get("hops") or []):
            oid = prior_hop.get("selected_id")
            if not oid:
                continue
            prior_opt = next((o for o in prior_hop.get("options") or [] if o.get("id") == oid), None)
            if not prior_opt:
                continue
            ident = _option_identity(prior_opt)
            match = next((o for o in hop.get("options") or []
                          if o.get("id") == oid or _option_identity(o) == ident), None)
            if match:
                hop["selected_id"] = match["id"]
            else:
                dropped.append(f"{leg.get('from')} → {leg.get('to')}: {prior_opt.get('name') or 'your pick'}")

    prior_stops = {s.get("stop_id"): s for s in prior_doc.get("itinerary") or []}
    prior_by_city = {(s.get("city") or "").strip().lower(): s for s in prior_doc.get("itinerary") or []}
    for stop in doc.get("itinerary") or []:
        prior = prior_stops.get(stop.get("stop_id")) or prior_by_city.get((stop.get("city") or "").strip().lower())
        if not prior:
            continue
        oid = (prior.get("hotels") or {}).get("selected_id")
        if not oid:
            continue
        prior_hotel = next((h for h in (prior.get("hotels") or {}).get("options") or []
                            if h.get("id") == oid), None)
        options = (stop.get("hotels") or {}).get("options") or []
        match = next((h for h in options if h.get("id") == oid), None)
        if not match and prior_hotel:
            name = (prior_hotel.get("name") or "").strip().lower()
            match = next((h for h in options if (h.get("name") or "").strip().lower() == name), None)
        if match:
            stop["hotels"]["selected_id"] = match["id"]
        elif prior_hotel:
            dropped.append(f"{stop.get('city')}: {prior_hotel.get('name') or 'your stay'}")

    doc["budget"] = compute_budget(doc)
    return dropped
