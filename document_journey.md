# Journey Scrapers — Flights & Hotels Reference

How the flight and hotel scraper stacks work: sources, location resolution,
sorting, filtering, the conditions that trigger each, and the optimizations we've
applied. Use this to audit behaviour and find the next optimization.

Last updated: 2026-06-26.

---

## 1. Architecture at a glance

```
                 /api/v1/search/{flights,hotels,all}      /api/v1/plan (journey)
                              │                                   │
                     FlightService.search              HotelService.search
                     (app/services/flights.py)         (app/services/hotels.py)
                              │                                   │
         ┌────────────┬───────┴───────┐          ┌────────┬───────┴────┬─────────┐
      Booking       EaseMyTrip     Gotogate    EaseMyTrip Booking    Agoda    Ixigo
   (parallel, per-source circuit breaker, shared rotating-proxy fallback)
```

Shared infrastructure (both stacks):
- **Circuit breakers** — `app/core/circuit_breaker.py`, one singleton per provider
  (`breaker_booking`, `breaker_emt`, `breaker_gotogate`, `breaker_agoda`,
  `breaker_ixigo`). Opens after N failures, returns the `fallback=[]` while open.
- **Proxy fallback** — `app/services/scraper_proxy.py` `with_proxy_fallback(label, do)`:
  try direct first, retry once via the rotating Webshare proxy on any failure or
  `ScraperBlocked`. Keeps proxy bandwidth near zero until the datacenter IP is blocked.
- **Browser impersonation** — `curl_cffi` `impersonate="chrome"` everywhere (bypasses
  Akamai / AWS WAF).
- **Static geo table** — `app/services/geo.py` (~373 cities: 182 India + 191 world).

---

## 2. Flights

### 2.1 Pipeline
`FlightService.search(origin, destination, date, adults, cabin_class, sort, nonstop_only)`:
1. Resolve `origin`/`destination` → IATA (once, shared by all sources).
2. Fan out to the 3 sources in parallel, each wrapped in its circuit breaker, with a
   30 s overall `asyncio.wait_for`.
3. Merge → dedup by `flight_number + departure_time` → apply `nonstop_only` → sort by
   price (priced first, unpriced last).
4. **No limit** — the full merged list is returned.

### 2.2 Sources

| Source | File | Endpoint | Sort | Nonstop filter | Booking URL |
|--------|------|----------|------|----------------|-------------|
| **Booking.com** | `booking/flights.py` | GET `flights.booking.com/api/flights/` | native `sort` param (`CHEAPEST`/`FASTEST`/`BEST`) — server-side | not sent (aggregator filters) | token deep-link |
| **EaseMyTrip** | `easemytrip/flights.py` | POST `…/AirBus_New` | client-side, price asc | `stops == 0` post-parse | listing deep-link |
| **Gotogate** | `gotogate/flights.py` | GraphQL `SearchOnResultPage` | client-side, price asc | `direct: true` **server-side** + client guard | `shareableUrl` |

All three search **one-way only** (`type=ONEWAY` / `isOneway` / single route).

**EaseMyTrip TKN handshake** (the non-obvious bit): `AirBus_New` rejects requests
without a valid `TKN` ("TOKEN NOT VALID"). We mint one per process (5-min cache) via a
2-step call to `gi.easemytrip.com/etm/api/etoken/`:
`UMS` (header `ATK`) → `{ITK, b}`, then `PSP` (header `autk = enc("EMT|<static>|<b>|<ITK>")`)
→ `{STK}`. `enc` = AES-128-CBC, key=iv=`hylW@zmEQdG@4Idr`, PKCS7, base64
(`_enc_fsearch`). A bare-string search response invalidates the token and triggers the
proxy retry. **If EMT rotates the key/ATK/static string, refresh them from the
flight-search bundle (chunk 732).**

### 2.3 IATA resolution (the single shared resolver)
`booking.resolve_iata(city)` — used by `FlightService.resolve_iata` and the journey
phase-2a. Order:
1. **Static geo table** `geo.iata_of(city)` — instant, offline, includes nearest
   airport for airport-less towns (Kasol→KUU, Rishikesh→DED, …).
2. **EMT autosuggest** (long tail), country-validated.
3. **Gotogate `searchLocations`** (fallback), country-validated.

**Country validation** (`_country_matches`): if `geo.country_of(city)` is known and a
live candidate's country (`geo.country_of_iata`) disagrees, reject it. This kills
cross-country garbage like `Kasol → KUL` (Kuala Lumpur). Results (incl. misses) cached.

### 2.4 Sorting & budget — `budget_to_sort` / `sort_flights` (`app/services/flights.py`)
| Budget | Sort | Key |
|--------|------|-----|
| low | cheapest | `CHEAPEST` (price asc) |
| mid | best | `BEST` = `price*0.6 + (duration + stops*60)*0.4` |
| high | fastest | `FASTEST` (duration asc) |

**Budget always wins**: when a `budget` tier is present it dictates sort and overrides
any explicit `sort`. Applied in `/search/flights` (effective_sort = `budget_to_sort(budget) or sort`)
and in `/plan` journey (`_pick_recommended_flight` + leg list re-ordered).

**Sort is pushed UPSTREAM, not just applied after merge.** A provider returns a
biased slice (e.g. top-20 by cheapest); sorting that slice locally can never surface
the fastest flights it never returned. So `FlightService.search` maps the effective
sort to each provider's native sort, then does a final merge-wide rerank:

| Provider | Native sort | Mapping |
|----------|-------------|---------|
| Booking | API `sort` param | `CHEAPEST` / `FASTEST` / `BEST` (passed straight through) |
| Gotogate | GraphQL `sortTypeCode` | CHEAPEST→`CHEAP_TRIP`, FASTEST→`SHORT_TRIP`, BEST→`RECOMMENDATION` |
| EaseMyTrip | none | local rerank only — but returns near-full inventory (~160), so safe |

Verified: `FASTEST` surfaces a 115-min flight that never appears in `CHEAPEST`'s top-5.

### 2.5 Conditions / gating
- **Domestic vs international** — `geo.is_domestic_iata`; unknown codes default to
  domestic. EMT `isDomestic` flag set from this (previously hardcoded `True`, which
  broke every international route).
- **Round-trip** — `/plan` (`plan.py`) runs `run_plan_journey` twice: "going"
  (origin→dest) and a separate "return" (dest→origin, swapped, `stay_nights=0`). All
  scrapers stay one-way; `return_date = departure_date + days`. Audited working
  (DEL→GOI going / GOI→DEL return).

### 2.6 Optimizations applied
- Single IATA resolution shared by all 3 sources (no per-scraper autocomplete fan-out
  — avoids latency + conflicting matches).
- Static geo table as primary resolver → removes the single-point-of-failure that once
  caused "no flights".
- Direct-first proxy usage; per-provider circuit breakers; 30 s fan-out cap.
- EMT token cached 5 min process-wide (handshake runs ~once, not per search).

---

## 3. Hotels

### 3.1 Pipeline
`HotelService.search(destination, checkin, checkout, rooms, adults, children, budget, travel_type, sort)`:
1. **Persona** = `derive_travel_type(adults, travel_type)` (explicit field, else 1→solo,
   2→couple, 3+→friends).
2. **Preferred property types** = `preferred_types(persona, budget)` (matrix below).
3. **Canonical sort** = `budget_to_hotel_sort(budget) or sort or "recommended"`.
4. Fan out to all 4 sources in parallel (circuit breakers), passing `sort` +
   `property_types` (+ `travel_type` to Agoda), `limit=_PER_SOURCE` (40, fixed).
5. Per-source: `_filter_by_budget` (currency-aware price band) → `_filter_by_distance`
   (≤20 km, keeps unknown-distance).
6. Merge → `_deduplicate_by_name` (richness-ranked) → `filter_and_rank_by_persona`
   (soft: keep preferred types, never empty; preferred ranked first).
7. **No limit** — full deduped list returned.

### 3.2 Sources & native capabilities (verified live 2026-06-26)

| Source | File | Endpoint | Native sort (`_SORTS`) | Native property filter | Location resolve |
|--------|------|----------|------------------------|------------------------|------------------|
| **EaseMyTrip** | `easemytrip/hotels.py` | POST `HotelListIdWiseNew` | `sorttype` (Price\|ASC, Popular\|DESC, Rating\|DESC) | ✅ `selectedProp` — Hostel/Hotel/Resort Property/Homestay/Apartment | ❌ raw city string (no autocomplete) |
| **Booking.com** | `booking/hotels.py` | GraphQL `FullSearch` | `selectedSorter` (price, class, bayesian_review_score, …) | ❌ GraphQL `filters` rejects `ht_id` → classifier | ✅ `resolve_dest_id` (country-validated, CITY→REGION→DISTRICT retry) |
| **Agoda** | `agoda/hotels.py` | GraphQL `citySearch` | `sortField` — **only** Price / Ranking / StarRating valid (others 400) | ❌ → classifier; `travellerType` from persona | ✅ `resolve_city_id` |
| **Ixigo** | `ixigo/hotels.py` | POST `accommodations/search` | `sortCriteria` — only `SC_P` verified | ❌ → classifier | ✅ `resolve_location` (seed + live) |

**Key facts surfaced by the live check (Goa):** Booking & Agoda surface hostels by
name; EMT carries hostels **only when `selectedProp` is set** (default mix has none);
EMT exposes `category`; Ixigo exposes `couple_friendly`.

### 3.3 Property-type classification — `hotels_filters.classify_property_type`
Canonical types: `hostel | homestay | guesthouse | hotel | resort | apartment`.
Priority (order matters — a wrong type is a user-facing trust bug):
1. **Explicit hostel tokens in the name** (hostel/backpacker/gostops/zostel/madpackers/
   hosteller/dorm…) — trusted **over** a wrong source category, so "Coco Hostel" filed by
   EMT under category "Hotel" still classifies as `hostel`.
2. Source structured category (EMT `category`).
3. Secondary name heuristics (resort/retreat, villa/homestay/cottage, apartment/serviced).
4. Default `hotel`.

Every normalized hotel is tagged with `property_type`. This is the **robust backbone**:
even where a source has no native type filter, classification + the persona soft-filter
delivers the right mix.

### 3.3a Destination normalization — `hotels_destination.normalize_destination`
The hotel-side equivalent of the flight IATA resolver (EMT hotels take a raw city
string with no autocomplete, so aliases/neighbourhoods silently miss). Returns a
`Destination(query, display_name, country, dest_class)`:
- `query` — one canonical string fed to **all four** sources. Resolves geo aliases
  (Panaji→goa, Bombay→mumbai) + hotel-specific neighbourhood aliases (Parvati Valley→
  kasol, Old Manali→manali, Triund→mcleodganj). Meaningful sub-regions (North/South Goa)
  are preserved.
- `dest_class` — `metro | beach | hill | town`, drives the adaptive distance radius.

### 3.4 Persona × budget matrix — `hotels_filters.preferred_types`
First entry = primary (drives native filter where supported); whole list drives the
soft filter + ranking. Couples & families **never** get hostels.

| | low | mid | high |
|--|-----|-----|------|
| **solo** | hostel, homestay, guesthouse, hotel | hostel, hotel | hotel, resort |
| **couple** | hotel, homestay, guesthouse | hotel, resort | resort, hotel |
| **friends** | hostel, homestay, hotel | hotel, hostel | resort, hotel |
| **family** | hotel, homestay, guesthouse | hotel, resort | resort, hotel |

Honors the rules: solo+low→hostels; mid+couple→hotels; high+couple/friends→resorts/hotels (no hostels).

### 3.5 Sorting & budget — `budget_to_hotel_sort`
| Budget | Canonical sort | Meaning |
|--------|----------------|---------|
| low | `price_asc` | cheapest first |
| mid | `recommended` | popularity / relevance |
| high | `rating` | rating / star-class first |

Each scraper's `_SORTS` maps the canonical key → its native code. **Budget always wins**
at the endpoint (`effective_sort = budget_to_hotel_sort(budget) or body.sort`),
re-applied by `_sort_hotels` for the final user-facing order.

### 3.6 Filters & conditions
- **Budget price band** — `_filter_by_budget`, currency-aware (`_BUDGET_PRICE_RANGE`),
  soft (never empties). Applied per source.
- **Distance (context-aware)** — `_filter_by_distance_adaptive`, applied once after
  merge. Base radius by `dest_class`: metro 18 km, beach 28 km, hill 10 km, town 14 km
  (explicit `max_distance_km` overrides). **Auto-widens** 2× then 3× if fewer than 6
  hotels survive, else keeps all — so a tight hill-town radius never empties results.
  Unknown-distance hotels are always kept.
- **Persona property type** — native (`selectedProp` on EMT; `travellerType` on Agoda) +
  client soft-filter for all.
- **Dedup** — `_deduplicate_by_name`, keeps the richest entry (booking_url > price > photos).
- `/plan` hotels: checkin = depart+1, rooms = ceil(travelers/2), skipped if stay ≤ 0 or > 30 nights.

### 3.7 Optimizations applied
- Native sort on all 4 + native property filter on EMT (hostels surface that otherwise
  wouldn't); classifier guarantees correctness where native filters are unavailable/fragile.
- Persona soft-filter never empties results (falls back to full set).
- Fixed `_PER_SOURCE=40` fetch (Booking self-caps at 25) — no user limit, no truncation.
- Agoda `travellerType` now persona-driven (was hardcoded "Couple").

---

## 4. Verified behaviour (Goa, 2026-07-15→17)

- **Flights** DEL→BOM: Booking ✓15 EMT ✓164 Gotogate ✓10 → 175 merged; budget low→cheapest,
  high→fastest (distinct top flight). Kasol→KUU (not KUL).
- **Hotels** solo+low → 69, 39 hostels ranked first; couple+high → 56, **0 hostels**,
  resorts first, all 4 sources; low budget → prices ascending; full list (67) not capped.

---

## 5. Optimization checklist

Done (2026-06-26):
- [x] Push flight sort upstream (Booking `sort` + Gotogate `sortTypeCode`); final merge rerank.
- [x] Classifier trusts explicit hostel name tokens over EMT category ("Coco Hostel"→hostel).
- [x] Hotel destination normalization layer (canonical query + dest class) feeding all sources.
- [x] Context-aware distance radius by dest class + auto-widen on sparse results.

Future:
- [ ] `BEST` score mixes INR with minutes → price dominates; reweight if duration/stops
      should matter for `mid`.
- [ ] Cache flight + hotel results per (route/city, date) for a short TTL.
- [ ] Native property filter only on EMT. Revisit Booking GraphQL `filters` shape and
      Agoda `idsFilters` / Ixigo `filterCriteria` accommodation-type ids to surface
      hostels/resorts harder at the source.
- [ ] Ixigo only price-sorts natively (`SC_P`); verify rating/popularity codes.
- [ ] Enrich `Destination` with lat/lng + source ids for true neighbourhood/zone filtering
      (replace raw-distance with travel-area relevance).

## 6. Key files
- Flights: `app/services/flights.py`, `app/services/{booking,easemytrip,gotogate}/flights.py`
- Hotels: `app/services/hotels.py`, `app/services/hotels_filters.py`,
  `app/services/hotels_destination.py`, `app/services/{booking,easemytrip,agoda,ixigo}/hotels.py`
- Shared: `app/services/geo.py`, `app/services/scraper_proxy.py`, `app/core/circuit_breaker.py`
- API: `app/api/v1/search/{endpoint,models}.py`, `app/api/v1/plan_trip/{plan,journey}.py`
