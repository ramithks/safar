# TravBuddy Growth OS — Workspace Polish, Community/Referral, SEO Scale

*Written 2026-07-16. Companion to `docs/trip_engine_architecture.md` (engine v4,
shipped). This plan covers the product/growth layer: workspace UX, the
community + referral flywheel, and organic-traffic scale.*

**Goal:** 66 indexed pages / 100–200 daily impressions → 10,000+ impressions
(100–200 organic users/day), and every system (trips, groups, community,
referral, hotels) interconnected — one operating system, not silos.

## Current state (surveyed, not assumed)

| Area | Reality |
|---|---|
| Workspace overview | TripActions shows budget switch + date + regenerate (just shipped); map renders in overview; layout is stacked cards |
| Transport/stays lists | No user-facing sorting — ranked order only |
| /community | Waitlist funnel (travel-style quiz) — no card, no referral, no live trips |
| Groups backend | FULL API exists: create, join by invite code, preview, messages, expenses (`app/api/v1/groups/`) |
| Referral/credits | Nothing exists (no tables, no API, no UI) |
| Hotels SEO | `/hotels/[slug]` pages EXIST (metadata + JSON-LD); master table 4,856 hotels, only 456 have `hotel_profiles`; sitemap caps hotels at 500 |
| Sitemap | Programmatic system exists (seo_destinations/routes/itineraries + hotels + weekend), publishes in curated waves |

---

## Phase 1 — Workspace: simple & sexy (frontend only, no backend risk)

**1a. Overview simplification**
- Remove the map from the overview tab (keep it on itinerary/day views).
- `TripActions` → `RouteBuilder`: keep ONLY the route breadcrumb (origin →
  stops → destination, removable chips + nights) and the add-stop
  autocomplete. Primary CTA: **"Add stop & rebuild trip"** — one clear verb.
- REMOVE from overview: budget low/mid/high switch, date picker, regenerate
  button. (Backend `/actions` keeps supporting them — the API stays; the
  overview stops exposing them. Date/budget live on in the Expert sheet
  later if wanted.)
- Overview hierarchy redesign: hero (destination, dates, travelers, live
  total) → route builder → recommended package (the ONE next action) →
  weather strip. Tighter spacing, fewer borders, one accent color, no
  duplicated information between cards.

**1b. Sorting**
- Transport (journey-board): sort control per leg — `Recommended (default) ·
  Cheapest · Fastest · Earliest departure`. Client-side over already-loaded
  items; picked ride stays pinned visible.
- Stays (stop-stays + hotels section): `Recommended · Price ↑ · Rating ↓`.
  Client-side, same pattern, shared `SortMenu` component.

*Files: `components/workspace/trip-actions.tsx` (→ route-builder),
`workspace-client.tsx`, `journey-board.tsx`, `stop-stays.tsx`,
`hotels-section` in `recommended-package.tsx`, new `sort-menu.tsx`.*

## Phase 2 — Referral + credits backbone (the interconnect)

Backend first — everything in Phases 3–5 hangs off this.

- **Tables** (one migration): `referral_codes` (user_id PK, code UNIQUE —
  short human code e.g. `SAHIL-4X2`), `credit_ledger` (id, user_id, amount,
  reason enum: `referral_signup | referral_reward | group_trip_bonus |
  redemption`, ref_id, created_at). Balance = SUM(ledger) — never a mutable
  column.
- **Rules**: new user signs up with a code → +₹500 ledger row for them
  (`referral_signup`) and +₹500 for the code owner (`referral_reward`),
  awarded when the referred user completes their first trip/group join (not
  at signup — fraud guard). One redemption per new user; self-referral
  blocked; codes rate-limited.
- **API**: `GET /api/v1/me/referral` → {code, balance, referred_count,
  pending}; `POST /api/v1/referral/redeem` {code} (authed, once);
  `GET /api/v1/groups/live` → public, sanitized list of open group trips
  (name, destination, dates, member_count, invite preview) for the
  showcase rails.
- Credits surface as "₹500 off your next trip" — applied at booking/checkout
  later; the ledger is the source of truth now.

*Files: backend `migrations/`, `app/api/v1/referral/`, extend
`app/api/v1/groups/endpoint.py` with `/live`; website proxy routes.*

## Phase 3 — Community hub (/community revamp)

- **Hero**: "Become part of India's biggest travel community" — value props
  as cards: Get best deals · Meet strangers who travel like you · Join group
  trips · ₹500 off for you + ₹500 for your friend.
- **Membership card**: branded card component showing name, member number,
  their referral code + QR (deep link `travbuddy.in/community?ref=CODE`).
  **Download as PNG** (canvas render, `card_{code}.png`) for sharing on
  Instagram/WhatsApp — the card IS the referral vehicle.
- **Live group trips rail**: from `GET /groups/live` — destination, dates,
  seats, "Join" → existing invite flow. Empty-state: "Start one from any
  trip you plan."
- Keep the existing style quiz as the join step (it already collects
  preferences); joining mints the referral code + card.

*Files: website `app/community/*` (rebuild client), new
`components/community/membership-card.tsx` (+ canvas download), proxies for
referral + live groups.*

## Phase 4 — SEO scale: hotels + sitemap (66 → thousands of pages)

- **Profile coverage**: batch job generating `hotel_profiles` for the
  remaining ~4,400 hotels — muscle-tier LLM (flash-lite), seeded/deterministic,
  from data already in the master table (name/city/stars/reviews/amenities).
  Publish in **waves** (~300–500/week) per the existing curated-wave
  discipline so growth looks organic to Google.
- **Sitemap scale**: paginate past the 500 cap (offset loop over the hotels
  API); move to `generateSitemaps()` sharding (`/sitemap/hotels-0.xml`, …)
  before 2k URLs.
- **City hub pages**: `/hotels/{city}` listing pages (Udaipur, Jaipur, …) —
  ~50–100 new indexable pages that internally link every profile; breadcrumbs
  both ways (profile ↔ city ↔ /hotels).
- **Interlinking**: trip/destination pages link to city hotel hubs; hotel
  profiles link to `/plan?to={city}` (SEO page → product loop).
- Measure in GSC weekly; publish next wave only when the previous is being
  crawled cleanly.

*Files: backend `scripts/generate_hotel_profiles.py` (+ celery beat wave
task), website `app/sitemap.ts` → sharded, new `app/hotels/city/[city]/page.tsx`
(or `/hotels/[city]` route segment), `lib/hotels.ts` pagination.*

## Phase 5 — Homepage interconnect

- **Live group trips rail** on the homepage (same component as community).
- **Referral banner**: "Give ₹500, get ₹500" → /community.
- Community card teaser (blurred card + "claim yours").
- Nav: Community elevated from footer to header.

## Sequencing & dependencies

```
Phase 1  (frontend-only, ships immediately)
Phase 2  (backend backbone) ──► Phase 3 (community UI needs codes/live API)
                             └─► Phase 5 (homepage rails reuse Phase 3 components)
Phase 4  (independent — can run parallel to 2/3; waves continue for weeks)
```

Recommended order of work: **1 → 2 → 3 → 4 (kick off waves) → 5**, with
Phase 4's profile-generation waves continuing in the background afterwards.

## Success metrics

- Phase 1: overview interaction rate up, "what do I do next" support asks down.
- Phase 2/3: referral codes minted, card downloads, redemptions, group joins
  from /community.
- Phase 4: GSC indexed pages 66 → 1,000+ (then waves to ~5k), impressions
  10k+/day, hotel-page → /plan click-through.
- Phase 5: homepage → community/group CTR.

## Risks

- **Referral fraud**: reward on first *completed* action, not signup; one
  redemption per account; block self-referral; rate-limit redeem.
- **SEO bulk-publish penalty**: waves, not dumps (existing discipline — keep it).
- **LLM profile quality**: muscle-tier with a strict schema + spot-check each
  wave before publish; profiles are additive (page renders without one).
- **Groups privacy**: /live exposes only sanitized fields the creator opted
  into showcasing (add `is_public` flag to groups, default false).
