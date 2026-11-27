# Partner stays in hotel search

Safar's own partner inventory is now **pinned above** the searched OTA
results on every stays surface, tagged `Verified`.

The tag says the listing was published by Safar after its completeness
gates — the same promise `/stays` already makes ("Safar Verified Stays").
It is **not** the `partners.verified` column, which is a separate and stricter
check (no live partner has it set today). Apart from the tag and the placement,
a partner card behaves exactly like every other result: same CTA wording, same
new-tab hand-off — it just lands on our own `/stays/{slug}` page instead of an
OTA's.

## Why this isn't the thing we said we'd never do

`app/services/marketplace_match.py` states the rule the marketplace matcher
holds to: *ranking never moves on commission, plan tier or `is_featured` — a
paid boost inside an AI recommendation is exactly the OTA behaviour we're
positioning against.* That rule is intact, and this does not break it:

* **Nothing was re-ranked.** Partner rows are not scored against OTA rows and
  never enter the ranking. They are a separate group placed above it. The
  searched list below is ordered exactly as it was before.
* **The placement is disclosed.** Every partner row carries the `Verified` tag,
  and in the Hotel Planner they sit in their own headed section. A boost you
  can see isn't a boost, it's a section.
* **"Recommended" still means recommended.** `_tag_recommended(…,
  skip_partners=True)` keeps the AI pick on the best *searched* stay, so the
  recommendation never silently becomes the placement.
* **No partner outranks another for paying.** Inside the pinned group the order
  is verified → genuinely reviewed → cheapest. `commission_pct`, plan tier and
  `is_featured` are not read at all.

What justifies the placement is that these rows are a different product: the
booking completes on Safar, at the owner's price, with an owner we can
reach — not a hand-off to Booking.com.

## How it works

`app/services/partner_stays.py` → `partner_stays(destination, checkin,
checkout, rooms, …)`

1. Published `partner_listings` where `kind='hotel'` and destination/city match
   (one indexed read, memoised 60s per city).
2. Per candidate: `room_types` + `check_stay` (the same advisory availability
   pre-flight `/quote` and `/checkout` use). **Sold out for those nights → not
   shown.**
3. One read for published `listing_reviews`. A rating is emitted *only* if
   travellers actually left one; a partner's brand rating is never borrowed as
   the property's.
4. Mapped into the scraper row shape (`name`, `price_per_night`, `photos`,
   `booking_url`, …) so every existing card, map and serializer renders it
   unchanged. `booking_url` is `{site_url}/stays/{slug}` — our own page.
5. Capped at `MAX_PARTNER_STAYS` (3).

**Strike-through pricing** (`_discount`) is emitted in the same fields the OTA
parsers already use — `price_original` / `savings` / `discount_pct` — so one
card renders "₹3,600/night ~~₹3,900~~ 8% off" for partner and Agoda rows alike.
Only when the original genuinely exceeds the price on the card: the partner sets
`original_price` against `price_from`, but the card can be showing a cheaper
room type, and a strike-through that isn't a saving is a lie the DB constraint
can't catch. The website's `stayDiscount()` applies the same guard, and
deliberately ignores Booking's `price_original_stay` — that one is per STAY, and
striking it through a per-night price would advertise a discount several times
the real one.

**Photos must be absolutised** (`_photo_urls`). Partner uploads are stored in
`partner_listings.photos` as the website's own *relative* proxy path,
`/image/<base64url object id>` (see `lib/marketplace/media.ts` — the branded
indirection that keeps bucket identifiers out of the HTML). Every hotel card
treats a photo as an absolute URL and silently drops anything that isn't
(`^https?://`), so shipping the stored value renders a partner stay as a hotel
with no pictures, and the mobile app has no origin to resolve it against.
`{site_url}` + the stored path is the fix; the proxy still serves the bytes.

Injection is in `HotelService.search`, so **every** surface gets it from one
place: quick search, the trip planner, the workspace stays tab, and mobile.
The fetch runs beside the scrapers (no added wall time), has a 6s ceiling, and
any failure yields zero partner rows and an otherwise untouched search.

Two things the injection is deliberately ordered around:

* It happens **after** `_persist_master`, so partner listings are never written
  into the `hotels` master table as if they were scraped OTA inventory.
* It happens **after** filtering, so the persona/budget/facility heuristics
  can't drop our own inventory. Explicit `price_min`/`price_max` *are* honoured
  inside `partner_stays` — a pinned row that ignores a price the traveller
  typed reads as an ad, not a result.
* An OTA row for the same property is dropped (`merge_partner_stays`), so a
  partner who also sells on Booking.com doesn't appear twice at two prices.

## Staying pinned

Any re-sort has to re-pin, or the placement depends on which sort chip was last
tapped. `pin_partners()` is called from:

| Where | File |
|---|---|
| Canonical sort | `app/services/hotels_filters.sort_hotels` |
| Planner ranking | `app/api/v1/plan_trip/helpers.rank_hotels` |
| Landmark re-search (before the cap) | `app/trips_doc/hotel_search.py` |
| Workspace stays tab | `components/workspace/stop-stays.tsx` → `sortStays` |
| Classic itinerary stays list | `components/itinerary/TripItinerary.tsx` → `sortHotels` |
| Hotel Planner results | `components/planner/hotels-planner.tsx` |

`app/trips_doc/serialize.py` `_HOTEL_FIELDS` must keep `is_partner`,
`partner_name`, `listing_slug` and friends — trimming any of them turns a
bookable partner row back into an anonymous hotel.

## Website

* `components/partner-badge.tsx` — the `Verified` tag and `isPartnerStay()`.
  Never render a partner row without the tag.
* Partner rows open in a **new tab** like every other stay, and fire
  `funnel.bookingClick("partner_stay", …)` so direct-booking intent stays
  measurable separately from OTA click-outs.
* The Hotel Planner renders them in their own headed section above the results.

## Tests

`tests/test_partner_stays.py` (25) — sold-out rows dropped, no invented
ratings, price filters honoured, photo absolutisation, strike-through pricing
(including the original-below-price case), ordering, caps, pinning through every
sort, and a DB failure staying silent. `lib/partner-stays.test.ts` (11) —
client-side sorts keep partners on top and keep each row's original index (the
index the select action sends back), plus the `stayDiscount` guards.
