# StaySuite Hotel OS — commerce, negotiation, team and marketing

Written 2026-08-23, after reading the live code, the live Supabase schema
(`rdlozqvuywtgsodunait`) and the Vite operator UI in `~/Desktop/Safar_supply_os`.

Working document. Phases run top to bottom; boxes are ticked as they land, and
anything that turns out to be wrong is recorded here rather than quietly
dropped.

---

## 0. Audit — what already exists

The brief reads as though none of this is built. Most of the hard half is. This
section is here so no phase below rebuilds something that already works.

### 0.1 Already built, and reused unchanged

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
| Claim checking | `claims.check()` — a body may not assert a figure the offer does not authorise | `services/marketing/claims.py` |
| Hotel website | `hotel_websites` draft/published + custom domain + public site API | `services/pms/website.py` |
| Agent context sizing | classifier → `toolsets.describe_for_provider(task=…)`, −81% on FAQ turns | `services/ai/toolsets.py` |
| Readiness | `GET /properties/{id}/readiness` — named gaps, not a percentage | `endpoint.py:475` |
| Audit of AI actions | `agent_runs` / `agent_actions` with `result_id` provenance | — |

**Nothing in that list was rewritten.**

### 0.2 The real gaps, and what closed them

1. `room_types` had 8 useful columns → **§1.1**, 15 more.
2. No negotiation *floor* anywhere, only a hotel-wide percentage → **§1.4 + §2.3**.
3. No group pricing at all → **§1.5 + §2.2**.
4. No early check-in / late checkout rules → **§1.2 + §2.4**.
5. No structured meals → **§1.3 + §2.4**.
6. `services` had no pricing unit → **§1.7**.
7. No experiences → **§1.7**.
8. No hotel team directory (`partner_members.profile_id` is NOT NULL) → **§1.8**.
9. No notification routing, no escalation lifecycle → **§1.8 + Phase 6**.
10. **No StaySuite fee anywhere in the codebase** → **§1.9 + §2.1**.
11. No Meta Marketing API → **§1.10 + Phase 7**.
12. No hotel-config audit log → **§1.11 + §2.7**.
13. Cancellation policy a schemaless `jsonb` → **§2.4**.
14. **Rate plans could be created and never changed** — found in Phase 4.

### 0.3 Two decisions taken up front, and both held

**Nullable columns, non-null publish gate.** §2 asks that no core field be
optional; §36 says nothing may break. A `NOT NULL` constraint cannot serve both,
so the database records what is *known* and readiness decides what may be
*sold*. "Not applicable" is a stored value (`extra_bed: 'none'`), distinct from
null, which means nobody has answered.

**The model never computes a decision.** `negotiate()` returns one of four
verdicts and, where it allows, a real `quotes` row. The LLM has no arithmetic to
do and no floor to leak.

---

## Phase 1 — Data model and migrations ✅ DONE

Eleven migrations, applied to `rdlozqvuywtgsodunait` and mirrored into
`migrations/`. RLS on every new table; indexes on every hot foreign key. No new
security advisories.

- [x] **1.1 `a_room_is_described_well_enough_to_sell`** — adults/children split,
      bed count, extra bed and crib with prices, floor, view, smoking,
      accessible, connecting, bathroom, `photo_meta`. Every closed vocabulary
      checked; every column nullable.
- [x] **1.2 `a_hotel_decides_what_a_late_checkout_costs`** — `stay_time_rules`,
      one band per `(kind, until_time)`, unique so the answer cannot depend on
      insertion order.
- [x] **1.3 `breakfast_is_a_fact_not_a_sentence`** — `meal_options`. What a rate
      *includes* stays derived from `meal_plan`; this is what the meal *is*.
- [x] **1.4 `a_room_has_a_floor_not_just_a_ceiling`** — `negotiation_rules`,
      scoped property/room_type/rate_plan, **no backfill** so a hotel that has
      configured nothing keeps exactly today's behaviour. Read policy is
      `has_partner_perm(…, 'rates')`, not `owns_partner`: the floor is the one
      number whose leak costs money on every future negotiation.
- [x] **1.5 `three_rooms_is_a_different_price_from_one`** — `group_rate_tiers`,
      `group_policies`.
- [x] **1.6 `an_agent_rate_can_be_a_discount_from_the_public_one`** —
      `rate_plans.derived_from_plan_id`, validity dates, selling days,
      allocation.
- [x] **1.7 `what_a_hotel_sells_besides_a_bed`** — `services.price_unit` and
      friends; new `experiences` table.
- [x] **1.8 `the_hotel_team_is_people_not_logins`** — `hotel_team_members`,
      `team_notification_rules`, `agent_escalations`.
- [x] **1.9 `the_platform_fee_is_a_number_not_a_conversation`** —
      `partners.platform_fee_bps` (400), `reservations.platform_fee_paise` /
      `payout_paise`, backfilled for all 72 rows.
      ⚠ **The backfill failed on the first attempt**: `total_paise * 400` is
      integer × integer, which overflows a 32-bit integer at a booking total of
      about ₹53 lakh — *before* the division that would bring it back into
      range. Fixed with `::numeric` casts.
- [x] **1.10 `marketing_that_can_be_reused`** — `marketing_profiles`,
      `ad_audiences`, `ad_campaigns`, `ad_creatives`, with the
      `ad_campaigns_published_has_id` check constraint.
- [x] **1.11 `who_changed_the_floor_price`** — `hotel_config_audit`,
      append-only, no write policy (service role only).
- [x] `a_quote_remembers_which_rule_priced_it` — `quotes.group_tier_id`.
- [x] `an_escalation_is_a_thing_that_happened` — `agent_actions.result_type`
      gains `escalation`. Filed under `handoff` would have been wrong: a handoff
      gives the conversation away, an escalation keeps it while asking a
      question, and reports counting one as the other would understate how often
      the AI finished the conversation itself.
- [x] `a_selling_agent_can_answer_the_questions_guests_ask` — the nine new tools
      granted to the 14 existing guest and sales agents.

## Phase 2 — Commercial services ✅ DONE

- [x] **2.1 `services/pms/fees.py`** — 400 bps, integer paise, one rounding.
      `fee + payout == total` asserted over thousands of totals.
- [x] **2.2 `services/pms/rates.py`** — derived plans resolved per night; group
      tiers as a **rate** and not a discount. That is the load-bearing decision:
      as a discount a tier would compete with the negotiation ceiling and a
      hotel with a 10% allowance could never move a rupee on a group booking.
      As a rate it flows into gross, into `nightly_paise`, and into the GST slab.
- [x] **2.3 `negotiation.negotiate()`** — `allowed · counter · needs_approval ·
      rejected`. Every §10 guardrail is a branch, not a prompt instruction.
      `Decision` carries no field named after the floor.
- [x] **2.4 `services/pms/policies.py`** — cancellation ladders, refund
      calculation, arrival/departure bands, meal resolution, deposit rules.
- [x] **2.5 Pricing precedence** — negotiated → group → B2B/derived → calendar →
      base, and it falls out of the design rather than being enforced by a rule.
- [x] **2.6 `services/pms/readiness.py`** — declared checks, per-room and per
      section, `pct` capped at 99 short of complete.
      ⚠ **Found by running it against production**: the first version gated
      sales on every check marked `blocks: booking` and reported **0 of 4 rooms
      sellable** at Casa Mandala. Every room type live today predates those
      columns, so it would have taken the entire inventory dark on deploy. Two
      bars now — `sellable` (name, stock, price) and `readyToSell` (§2's bar).
- [x] **2.7 `services/pms/audit.py`** — one row per changed field, never fails
      the change it records.

## Phase 3 — APIs ✅ DONE

29 routes on a second router at the same `/pms` prefix, behind the same flag and
the same `require_member`.

- [x] Room detail list + PATCH; rate-plan detail list + **PATCH (new)**.
- [x] `stay-time-rules`, `meals`, `negotiation-rules`, `negotiation-preview`,
      `group-tiers`, `group-policy`, `experiences`, `services` DELETE, `team`,
      `notification-rules`, `escalations`, `readiness/full`, `config-audit`.
- [x] Marketing: `marketing-profile`, `audiences`, `ads`, `ads/drafts`,
      `ads/{id}/creative`, `ads/{id}/publish`.
- [x] ⚠ **A routing collision**, found by the e2e: `GET /room-types/detail`
      answered 405 because `PATCH /room-types/{id}` is registered first and
      `detail` matched as a room type id. Renamed to `room-types-detail`.

## Phase 4 — Partner dashboard UI ✅ DONE

- [x] **4.1** Ten-step room editor. Nothing says "optional"; "Not applicable" is
      a button; each step saves as it is finished; the rail's ticks are the
      server's readiness checks. A room goes on sale at step three and the
      wizard says so.
- [x] **4.2** Hotel → Meals / Arrival & departure / Services / Experiences.
- [x] **4.3** Pricing → Public / Agent / Groups / Negotiation / History, with
      "Try it on your own AI" running the real engine against the real calendar.
- [x] **4.4** Team → People / Who hears what / Asked.
- [x] **4.5** Marketing → Ads / Audiences / Who you're for.
- [x] **4.6** Nav regrouped: a "Your hotel" section for what a hotel *is*,
      separate from what it is doing today.

## Phase 5 — Agent retrieval and tools ✅ DONE

- [x] `get_room_details`, `get_checkin_options`, `get_meal_info`,
      `get_group_rates`, `get_cancellation_terms`, `get_payment_terms`,
      `get_experiences`, `negotiate_rate`, `escalate_to_team`.
- [x] `toolsets.py` menus extended, never-widens invariant re-asserted.
- [x] Prompt rules: what may never be said out loud.
- [x] Migration granting the tools to guest and sales agents only.

## Phase 6 — WhatsApp team escalation ✅ DONE

- [x] `services/pms/escalation.py` — routing (configured rules → role fallback →
      anyone reachable), a message composed server-side from stored facts, a
      dedupe key from substance rather than wording, a lifecycle the agent reads.
- [x] The internal message **may** name the floor; nothing sent to a guest ever
      may. That asymmetry is the reason it is composed on the server.
- [x] Failure to notify is recorded as failure: the escalation stays `open`, the
      dashboard shows it, and the agent is told to hand off for real.
- [x] Verified against the live database: routed to the configured member,
      deduped on the second attempt, message rendered correctly. The demo
      hotel's WhatsApp connection is `is_sandbox`, so nothing left the building.

## Phase 7 — Instagram and ads ✅ DONE

- [x] `services/marketing/creatives.py` — five templates, filled from structured
      data, **no model in the path**. A template with nothing true to say returns
      nothing.
- [x] Four formats: single image, carousel (ordered, per-card captions), video,
      and boosting an existing Instagram post.
- [x] `services/marketing/meta_ads.py` — the full request composed and
      validated, `not_connected` reported with the specific thing missing.
      §29 enforced in three places: the check constraint, this module, the
      screen.
- [x] ⚠ **A copy bug found by a test and then a worse one behind it**: generated
      copy ended "…with a private balcony over the…", and a description that
      *itself* ends on a dangling word was under the length limit and passed
      through untouched. The trim now runs on every path.
- [x] ⚠ **Two of Casa Mandala's stored photo URLs no longer resolve** and were
      offered as selectable ad images. Unloadable photos now leave the pool.

## Phase 8 — Tests and E2E ✅ DONE

| Suite | Count | What it proves |
| --- | --- | --- |
| unit | 1,621 | every rule, as properties rather than examples |
| `hotel_os_e2e` | 73 | the configuration surface over real HTTP, incl. 10 cross-tenant 404s |
| `negotiation_e2e` | 31 | the §44 conversation, deterministically |
| `onboarding_e2e` | 65 | regression — a stranger can still sign up and sell |
| `golden_e2e` | 88 | regression — discovery → booking → payment → check-in → bill |
| `sales_e2e` | 185 | regression — the agent, the PMS and the ledger |

## Phase 9 — Website, polish, responsive ✅ DONE

- [x] `GET /public/hotels/{slug}` now returns meals, experiences, arrival bands,
      the full room detail and resolved cancellation terms — §45's "the website
      uses the same structured data".
- [x] Mobile verified in a real browser at 400px: sidebar collapses, editor
      stacks, 44px targets, 16px inputs.
- [x] ⚠ Cover photos that 404 rendered as grey voids; they now fall back to the
      empty state.
- [x] ⚠ Room cards showed each gap's *consequence* sentence, which is right on
      the review step and wrong on a grid — three rooms missing the same three
      fields became three identical paragraphs. Short labels now.
- [x] ⚠ The audit log wrote seven rows for one save, four of them defaults
      nobody chose, and labelled a rule "room_type" in a table meant to be read
      by a hotelier. Both fixed.

---

## Not built, and why

* **Publishing to Meta.** StaySuite holds `instagram_business_basic` and
  `instagram_business_manage_messages` — DM scopes. Ads need `ads_management` on
  a Business account with a funded ad account, a different OAuth configuration
  and a different review. Everything up to the wire is built and tested; the one
  function that posts raises `NotImplementedError` and is unreachable.
* **Meta interest ids.** Audience interests are stored as the hotelier's words.
  Resolving them needs a search endpoint that requires the same credentials.
* **Carousel drag-to-reorder.** Order is the tap order, which works; drag is a
  refinement.
* **`unanswered` escalations.** Routed and composable, but nothing raises them —
  the sweep that would notice a waiting guest is not built.
* **AI-written ad copy.** Deliberately not built. See `creatives.py`.
