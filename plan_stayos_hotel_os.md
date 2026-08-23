# StayOS Hotel OS — commerce, negotiation, team and marketing

Written 2026-08-23, after reading the live code, the live Supabase schema
(`rdlozqvuywtgsodunait`) and the Vite operator UI in `~/Desktop/Travbuddy_supply_os`.

Working document. Phases run top to bottom; boxes are ticked as they land, and
anything that turns out to be wrong is recorded here rather than quietly
dropped.

---

## 0. Audit — what already exists

The brief reads as though none of this is built. Most of the hard half is. This
section is here so no phase below rebuilds something that already works.

### 0.1 Already built, and being reused unchanged

| Brief asks for | Already exists | Where |
| --- | --- | --- |
| One pricing source of truth | `price_stay()` — integer paise, per-night GST slab, discount before tax, token pct | `services/pms/pricing.py` |
| Deterministic quotes an LLM cannot invent | `quotes` — immutable, expiring, `nightly_paise`, server-computed | `services/pms/booking.create_quote` |
| B2B rates | `rate_plans.audience = 'b2b'`, `b2b_agents` contracts, credit line, agency portal | `services/pms/b2b.py` |
| Negotiation ceilings | `settings.max_discount_pct` / `max_approved_discount_pct`, clamped in `price_stay` | `services/pms/negotiation.py` |
| Approval workflow | proposal → approval → **revalidate** → execute | `services/ai/approvals.py` |
| Money truth | `money.state()` — one balance for every surface | `services/pms/money.py` |
| Booking snapshot | `reservations` stores gross/discount/tax/total/paid + `cancellation_policy` | `reservations` |
| Photo upload | GCS, byte-sniffed MIME, `POST /properties/{id}/media` | `services/pms/uploads.py` |
| Add-ons | `services` + `reservation_services` + folio posting | `services` |
| Team + permissions | `partner_members`, `ROLE_PERMS`, `require_member(perm)` | `deps/property.py` |
| Instagram | OAuth connect, webhook, conversations, agent runtime | `services/messaging/instagram/` |
| Outbound campaigns | `marketing_campaigns` — consent, frequency, per-recipient revalidation | `services/marketing/` |
| Hotel website | `hotel_websites` draft/published + custom domain + public site API | `services/pms/website.py` |
| Agent context sizing | classifier → `toolsets.describe_for_provider(task=…)`, −81% on FAQ turns | `services/ai/toolsets.py` |
| Readiness | `GET /properties/{id}/readiness` — named gaps, not a percentage | `endpoint.py:475` |
| Audit of AI actions | `agent_runs` / `agent_actions` with `result_id` provenance | — |

**Nothing in the list above is rewritten.** Every new thing below either adds a
column to one of those tables or adds a table that one of those services reads.

### 0.2 The real gaps

1. `room_types` has 8 useful columns. No adults/children split, no bed count, no
   extra bed, no crib, no floor/view/smoking/accessible/connecting, no bathroom.
2. **Nothing anywhere holds a negotiation floor as a price.** Only a property-wide
   discount *percentage*. Per-room and per-rate-plan floors do not exist, and
   there is no tool that decides a counter-offer — the model would have to.
3. **No group pricing at all.** `units` is priced linearly; four rooms cost four
   times one room.
4. **No early check-in / late check-out rules.** `check_in_time` is a single time.
5. **No structured meals.** `rate_plans.meal_plan` is EP/CP/MAP/AP and nothing
   says what breakfast costs, when it is served, or whether it is a buffet.
6. `services` has no pricing unit, no hours, no max quantity, no photo, no terms.
7. **No experiences.**
8. **No hotel team directory.** `partner_members` is a login list keyed on
   `profile_id NOT NULL` — a front-desk WhatsApp number with no account cannot
   exist in it.
9. **No notification routing, and no escalation lifecycle.** `request_human_handoff`
   flips `conversations.handler` and raises a task. Nobody is messaged.
10. **No StayOS fee anywhere in the codebase.** Grepped: no `platform_fee`,
    no `payout`, no `settlement`. The 4% exists only in conversation.
11. **No Meta Marketing API.** `graph.facebook.com` appears only for *messaging*.
    There is no ad account, no campaign, no creative, no audience.
12. **No hotel-config audit log.** `admin_audit_log` is for TravBuddy staff
    actions, not for a hotelier changing a floor price.
13. Cancellation policy is a free `jsonb` with no schema and no resolver.

### 0.3 Two decisions taken up front

**Nullable columns, non-null publish gate.** The brief says no core field may be
optional. Making the columns `NOT NULL` would break the 21 room types and 10
properties that already exist, and §36 says nothing may break. So every new
column is nullable in Postgres, and *readiness* — a deterministic server-side
check — is what decides whether a room may be sold. A room missing its
cancellation policy is not rejected by the database; it is `sellable: false`
with a named reason, and the booking engine refuses it. "Not applicable" is a
real stored value (`extra_bed: 'none'`, `children: 'not_allowed'`), distinct
from null, which means nobody has answered yet.

**The model never computes a price, and now it never computes a *decision*
either.** `negotiate_rate()` returns one of four verdicts and, where it allows,
a real `quotes` row. The LLM is handed a sentence and a quote id. It has no
arithmetic to do and no floor to leak.

---

## Phase 1 — Data model and migrations

Nine migrations, each named for what it makes possible. Applied to
`rdlozqvuywtgsodunait`, mirrored into `migrations/` as files.

- [ ] **1.1 `a_room_is_described_well_enough_to_sell`** — `room_types` gains
      `max_adults`, `max_children`, `bed_count`, `extra_bed` (`available` ·
      `on_request` · `none`), `extra_bed_paise`, `crib` (same vocabulary),
      `crib_paise`, `floor`, `view`, `smoking`, `accessible`, `connecting`,
      `bathroom` (`private` · `shared`), `children_allowed`. Check constraints
      on every closed vocabulary. `size_sqft` already exists and stays.
- [ ] **1.2 `a_hotel_decides_what_a_late_checkout_costs`** — `stay_time_rules`:
      one row per band per kind, `(property_id, kind, until_time)` with
      `charge_kind` ∈ `free · fixed · pct_of_night` and `amount`. Bands are
      evaluated in time order; the first band whose `until_time` is not passed
      wins. Hotel-level, because that is how hotels actually price it.
- [ ] **1.3 `breakfast_is_a_fact_not_a_sentence`** — `meal_options`: per property
      per meal, `available`, `price_paise`, `price_unit`, `starts_at`, `ends_at`,
      `diet` (`veg` · `nonveg` · `both`), `service_style`. What a *rate plan*
      includes stays derived from `meal_plan` — one vocabulary, not two.
- [ ] **1.4 `a_room_has_a_floor_and_a_target_not_just_a_ceiling`** —
      `negotiation_rules`: scope `property · room_type · rate_plan`, with
      `target_paise`/`target_pct`, `floor_paise`/`floor_pct`, `ceiling_paise`,
      `auto_pct`, `approval_pct`, `valid_from`, `valid_to`, `days_of_week`,
      `blackout_dates`, `enabled`. Most specific scope wins. The property-level
      row is written from the existing `settings.max_discount_pct` by the
      migration itself, so no hotel loses the ceiling it already had.
- [ ] **1.5 `three_rooms_is_a_different_price_from_one`** — `group_rate_tiers`
      (`min_units`, `max_units`, `discount_pct` | `fixed_price_paise`,
      `negotiable`) and `group_policies` (min group size, deposit pct, payment
      deadline days, cancellation terms, blackout dates, meal package).
- [ ] **1.6 `an_agent_rate_can_be_a_discount_from_the_public_one`** — `rate_plans`
      gains `derived_from_plan_id`, `derive_discount_pct`, `valid_from`,
      `valid_to`, `days_of_week`, `allocation_units`. A derived plan has no
      base price of its own; `nightly_for_plan` resolves it against its parent
      per night, so a seasonal public rate moves its B2B rate with it.
- [ ] **1.7 `what_a_hotel_sells_besides_a_bed`** — `services` gains
      `price_unit`, `availability`, `opens_at`, `closes_at`, `max_quantity`,
      `available_from`, `available_to`, `photo_url`, `terms`, `advance_required`.
      New `experiences` table with its own booking shape (duration, meeting
      point, min/max participants, age limits, requirements, photos, policy).
- [ ] **1.8 `the_hotel_team_is_people_not_logins`** — `hotel_team_members`
      (name, role, department, whatsapp, phone, email, active, optional
      `member_id` → `partner_members`), `team_notification_rules` (event →
      member → channels), `agent_escalations` (conversation, kind, dedupe key,
      payload, member, status `open · notified · responded · resolved`,
      resolution, timestamps).
- [ ] **1.9 `the_platform_fee_is_a_number_not_a_conversation`** —
      `partners.platform_fee_bps` default 400; `reservations` gains
      `platform_fee_paise` and `payout_paise`, both snapshotted at creation and
      never recomputed. Backfill computes them for the 72 existing rows from
      their own stored totals at 400 bps, so historical settlement is stable.
- [ ] **1.10 `marketing_that_can_be_reused`** — `marketing_profiles`,
      `ad_audiences`, `ad_campaigns`, `ad_creatives`.
- [ ] **1.11 `who_changed_the_floor_price`** — `hotel_config_audit`, append-only,
      RLS read-only to members with `rates`, written service-side.

RLS on every new table, following the existing partner/property pattern exactly.
Indexes on every foreign key a hot path filters by.

## Phase 2 — Commercial services (no UI, no agent)

- [ ] **2.1 `services/pms/fees.py`** — `breakdown(total_paise, bps)` → subtotal,
      fee, payout. One function, used by booking, payments, receipts, dashboard
      and settlement. Never shown to a guest.
- [ ] **2.2 `services/pms/rates.py`** — resolve a derived rate plan per night;
      apply group tiers to a `units` count; return which rule applied and why.
- [ ] **2.3 `services/pms/negotiation.py` extended** — `resolve_rules()` (scope
      precedence), and `negotiate()` returning `allowed · counter · rejected ·
      needs_approval` with `final_paise`, `reason`, `expires_at`. Every guardrail
      in §10 is a branch here, server-side.
- [ ] **2.4 `services/pms/policies.py`** — structured cancellation ladder with a
      validated schema, a refund calculator, and one resolver both the agent and
      the website read. `stay_time_rules` evaluator lives here too.
- [ ] **2.5 Pricing precedence, stated once** — negotiated → group → B2B →
      calendar → plan base. Written as a single ordered function in
      `booking.create_quote`'s path so no second implementation is possible.
- [ ] **2.6 Readiness v2** — deterministic section checks, and `sellable` on a
      room type gating the booking engine.

## Phase 3 — APIs

Every route on the existing `/api/v1/pms` router, under the existing
`require_member(perm)` model, with the existing rate limiter.

- [ ] Room types: full PATCH surface for the new fields (PATCH already exists).
- [ ] `stay-time-rules`, `meals`, `negotiation-rules`, `group-tiers`,
      `group-policy`, `experiences`, `team`, `notification-rules`,
      `marketing-profile`, `audiences`, `ad-campaigns` — list/upsert/delete.
- [ ] `POST /properties/{id}/negotiate` — the same deterministic engine the
      agent tool calls, so the dashboard can show a hotelier exactly what their
      AI would answer.
- [ ] `GET /properties/{id}/readiness` extended with sections.
- [ ] Media: reorder, delete, primary, alt text, caption.

## Phase 4 — Partner dashboard UI

- [ ] **4.1** Ten-step room editor replacing the one-card form, with a completion
      rail, per-step validation, and explicit *Not applicable* rather than blank.
- [ ] **4.2** Hotel → Profile / Photos / Amenities / Policies / Meals / Services /
      Experiences.
- [ ] **4.3** Pricing → Public / B2B / Group / Negotiation, with a live
      "what your AI would say" preview driven by the real engine.
- [ ] **4.4** Team → Members / Directory / Notifications.
- [ ] **4.5** Marketing → Profile / Audiences / WhatsApp ad / Instagram ad.
- [ ] **4.6** Navigation regrouped per §39; readiness surfaced as named gaps.

## Phase 5 — Agent retrieval and tools

- [ ] New tools, each a thin call into Phase 2: `get_room_details`,
      `get_checkin_options`, `get_meal_info`, `get_services`, `get_experiences`,
      `get_group_rate`, `negotiate_rate`, `get_cancellation_terms`,
      `get_payment_terms`, `escalate_to_team`.
- [ ] `toolsets.py` — new tasks (`negotiation`, `group`, `policy`, `addons`) and
      their menus, keeping the never-widens invariant.
- [ ] Prompt rules: what may never be said out loud (floor, target, fee, payout,
      internal notes, team detail).
- [ ] Migration granting the new tools to the guest and sales agents only.

## Phase 6 — WhatsApp team escalation

- [ ] Structured internal message built server-side from the escalation payload —
      the model supplies a reason, never the numbers.
- [ ] Routing by event kind → `team_notification_rules` → member's WhatsApp.
- [ ] Dedupe on `(conversation_id, kind, dedupe_key)`; lifecycle transitions;
      the agent can read escalation state and does not re-escalate.
- [ ] Approval loop: hotel replies → approval executes through the existing
      `approvals.py` revalidation path.

## Phase 7 — Instagram and ads

- [ ] Instagram management screen over the existing connection + conversations.
- [ ] Ad studio: pick room/experience/offer, pick media from the hotel's own
      uploaded photos or an existing IG video, choose single / carousel / video,
      generate headline + primary text + CTA from **structured data only**, and
      let the hotelier edit every field.
- [ ] Caption and creative templates, editable, stored per property.
- [ ] Publish adapter: real Meta Marketing API where credentials exist, and an
      honest `not_connected` state where they do not. `draft · connected ·
      published · running · paused · failed` are distinct and never faked.

## Phase 8 — Tests and E2E

- [ ] Unit suites per service, with the negotiation guardrails as properties
      rather than examples.
- [ ] `scripts/hotel_os_e2e.py` — the §44 checklist against a real deployment.
- [ ] The §44 conversation, end to end: "₹5,000 ka room hai, 4 rooms chahiye,
      best price kya doge?"

## Phase 9 — Polish, responsive QA, final report
