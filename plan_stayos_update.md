# StayOS Agent 2.0 — audit and plan

Written 2026-08-22, after inspecting the live code and the live Supabase data.
Working document: phases are executed top to bottom, boxes ticked as they land.

---

## 0. Audit — what already exists

The brief assumes a system that has to be built. Most of it is built. This
section records what was found so no phase below rebuilds something.

### 0.1 Where things live

| Thing | Where |
| --- | --- |
| Agent runtime, tools, messaging, PMS | `~/Desktop/travbuddy_backend` (FastAPI) |
| StayOS operator UI + guest surfaces | `~/Desktop/Travbuddy_supply_os` (Vite SPA, `partner.travbuddy.in`) |
| Consumer site | `~/Desktop/Travbuddy_website` (Next.js, `travbuddy.in`) |
| **StayOS database** | Supabase `rdlozqvuywtgsodunait` ("Travbuddy agency") |
| Marketplace/consumer database | Supabase `uabxtdhxbamuhthxnhid` ("Travbuddy") |

`enable_hotel_os` gates the PMS routes; StayOS is its own deployment of the same
codebase with `.env.hotelos`.

### 0.2 The Instagram path, as it actually runs

```
Meta ──► POST /api/v1/messaging/instagram/webhook   (api/v1/messaging/webhooks.py)
      ──► verify HMAC (instagram/webhook.py: verify_signature)
      ──► parse envelope: entry[].messaging[], recipient.id = us
      ──► messaging.resolve_connection(account_id, "instagram")   → instagram_connections → property
      ──► messaging.record_inbound()                              → conversations + conversation_messages (idempotent on mid)
      ──► 200 to Meta
      ──► pipeline.dispatch() ──► Celery ──► process_conversation()
              ├─ Redis lock per conversation, drains unanswered messages in order
              ├─ agent_may_answer()  (handler / ai_enabled)
              ├─ routing.route()     → agent_type + actor
              ├─ billing.allowed()   → credit gate
              ├─ ai_context.load_context()  → AgentPrincipal + agent_configs + agent_tools
              ├─ _history()          → last 10 delivered message bodies
              └─ runtime.run_turn()
                    ├─ classify.ModelClassifier → task  (flash-lite, skipped for greetings)
                    ├─ model_router.select(task)  → tier/model/thinking budget
                    ├─ agent_runs row opened first
                    ├─ tool loop → registry.execute → agent_actions row per call
                    ├─ guardrails.check_reply()   → links / prices / "you are booked"
                    └─ messaging.send()
```

**The brief's requirement — "I pass only the new message and the Instagram user
id; the backend retrieves everything else" — is already the design.** Nothing
about conversation history is sent by Meta and nothing is expected from it.

### 0.3 What the brief asks for that already exists

- **Intent router** — `services/ai/classify.py`, per-agent candidate lists, cheap
  tier, greeting short-circuit, failure falls back rather than erroring.
- **Model tiering** — `services/ai/model_router.py`: conversation / reasoning /
  classify, capability→tier table, no model id anywhere else.
- **Deterministic pricing** — `quotes` are server-computed, immutable, expiring,
  with `nightly_paise`; the LLM cannot name a price.
- **Holds** — `inventory_holds`, taken by `get_quote`.
- **Webhook-driven payment** — `payment_orders` is the idempotency anchor;
  `payments.claim()` makes the browser callback and the Razorpay webhook race
  safely; `_fulfil_booking` runs exactly once.
- **Booking state** — `reservations.status` + `reservations.payment_status`
  (`token_paid` etc.), plus `quotes.status`, `payment_orders.status`. The
  lifecycle exists; it is spread over three tables rather than one enum.
- **Audit trail** — `agent_runs` / `agent_actions`, refusals recorded,
  `agent_action_id` provenance both ways.
- **Guardrails** — invented links, invented prices, placeholder text, and
  (as of `a98a70b`) invented booking confirmations in EN/HI/Hinglish.
- **Session state (partial)** — `quotes.conversation_id` is written, and
  `prompts._live_quote()` reads it back into the system prompt.
- **Hotel structured facts** — `property_knowledge` (Q&A + keywords),
  `properties.policies`, `room_types.{description,bed_type,size_sqft,amenities,photos}`.
- **Permission model** — `AgentPrincipal` is a `Member` look-alike; per-tool
  grants in `agent_tools`; reservation/task subject checks in `AgentContext`.

### 0.4 The reference conversation, decoded

Instagram user `1376629097955726` →
`conversations.external_id = '1376629097955726'`,
id `e58d75c2-b616-4f2d-a016-70e1fed73768`, property `harry` (bhiwani),
`handler = 'human_requested'`, `guest_id = NULL`.

44 messages, 22 `agent_runs`, 12 `agent_actions`. Six of its bugs were fixed in
commit `a98a70b` ("Six bugs from one real Instagram conversation"): the invented
booking claim, the lost quote id, the self-blocking hold, the hallucinated
`rate_plan_id = "breakfast_included"`, "photos nahi dikha sakta", and the
greeting answered with an intake form.

**What that commit did not fix, and what this plan is for:**

The guest paid. `payment_orders 0c5eecea…` — `status = paid`, `paid_at
19:58:52`, `razorpay_payment_id pay_TSXkzPHIWDrGO3`. `reservations
6585c7ea…` — `ref TB-5CECKV`, `status confirmed`, `payment_status token_paid`,
₹35,840 total, ₹7,168 paid.

26 seconds later the guest wrote "Payment hogyi". The agent replied *"Aapki
booking tab confirm ho jaayegi jab payment process ho jaayegi"* — the booking was
already confirmed — and on the next message the guardrail escalated the whole
conversation to a human.

Root causes, all still live:

1. **Nothing links the payment back to the conversation.** `payment_orders` has
   no `conversation_id`; `reservations` has no `conversation_id`.
2. **Nothing links the guest.** On Instagram, `_upsert_conversation` looks up
   `guests` by `ilike(phone, %handle%)` — a handle is not a phone number, so
   `conversations.guest_id` is `NULL` forever, `_link_reservation` never runs,
   and `list_guest_reservations` returns nothing.
3. **There is no payment/booking status tool at all.** Full tool inventory
   (67 tools) contains `get_reservation`, `find_reservation`,
   `list_guest_reservations` — nothing that answers "did my payment land".
4. **Nobody tells the guest when the money arrives.** `_fulfil_booking` creates
   the reservation and stops. No outbound message, no confirmation link.

### 0.5 Other confirmed gaps

- **Language preference is not stored.** Guest asked for Hinglish at 18:46:31;
  the agent replied in Hindi at 18:48:36 and again at 19:57. `conversations`
  has an unused `guest_profile jsonb` column and `guests.preferences jsonb` is
  read by `get_guest` and never written by anything.
- **User memory does not exist.** No memory table, no extraction.
- **Zero room photos in production.** All 10 properties, 21 room types,
  `array_length(photos,1) = 0` everywhere. 2 of 21 room types have a
  description. The photo tool now works and has nothing to return.
- **No AI hotel/room extraction.** `services/pms/onboarding.py` is manual
  create-hotel / add-room-type / add-rate-plan; the Vite `add-room.tsx` is a
  form. (The *marketplace* has `ai_import_jobs` on the other project — a
  different product, not reusable as-is.)
- **`properties.lat/lon` is null for 9 of 10 properties**, `policies` empty for 7.
- **The hotel website has no SEO or AEO at all.** `/stay/:slug` is a lazy React
  route in a Vite SPA. `index.html` ships one static
  `<title>TravBuddy Partner</title>`, no per-hotel metadata, no server-rendered
  content, no JSON-LD, no sitemap, no canonical. To Google and to any answer
  engine, every StayOS hotel website is one blank page.
- **`razorpay_webhook_events` has RLS disabled** (flagged by the advisor).
- **Booking confirmation page**: `/pay/:orderId` shows a result but there is no
  durable, tokenised "view my booking + receipt" URL to send on Instagram.
  `/checkin/:ref` exists and is keyed on the booking reference.

### 0.6 Cost/latency baseline (from `agent_runs` on this conversation)

| | |
| --- | --- |
| tokens_in per turn | 2,550 – 11,131 (median ≈ 3,000) |
| latency | 2.1 s – 13.2 s |
| model | `gemini-2.5-flash` on every turn (no turn reached the reasoning tier) |
| classification | one extra flash-lite call per non-greeting turn |

The big turns are the ones where `get_stay_options` output (all room types × all
rate plans × nightly breakdown) goes back into the prompt.

---

## Phase 1 — The money is the truth, and the guest hears it ✅ DONE

Fixes the single worst failure: the guest paid and the AI did not know.

- [x] **1.1** Migration `a_payment_knows_which_conversation_it_came_from`:
      `payment_orders.conversation_id`, `reservations.conversation_id`, both
      nullable with partial indexes. Applied.
- [x] **1.2** `quote_payment.create_link(conversation_id=…)` →
      `payments.create()`; `create_booking_payment_link` passes
      `ctx.conversation_id`. A reused open order is stamped once if it has no
      thread yet, and never reassigned.
- [x] **1.3** `booking.create_reservation(conversation_id=…)`, falling back to
      `quotes.conversation_id`; `_fulfil_booking` passes the order's thread and
      then calls `messaging.link_booking()` — which sets
      `conversations.reservation_id`, `guest_id` and `guest_name`, and refuses
      to overwrite a thread that already names a stay.
- [x] **1.4** `get_booking_status()` — **no arguments at all**, which is the
      security property: there is no phrasing that reaches another guest's
      stay. Composes `reservations` → `payment_orders` → `quotes`, newest fact
      first, into `NONE · QUOTE_ACTIVE · PAYMENT_PENDING · PAYMENT_RECEIVED ·
      PAYMENT_FAILED · CONFIRMED · CHECKED_IN · CHECKED_OUT · CANCELLED`, each
      with the sentence the model is allowed to build on.
- [x] **1.5** Guardrails: `_read_a_real_booking()` licenses a "you are booked"
      claim from the tool's *output*, not its name — `PAYMENT_PENDING` still
      refuses, which is the state it runs in most of the time.
- [x] **1.6** `messaging.announce_booking()` — proactive confirmation in
      en/hi/hinglish, idempotent on `client_reference = booking:{id}`, never
      raises. WhatsApp falls back to the `booking_confirmation` template
      outside the window; Instagram has no template mechanism and says so in a
      log line rather than pretending.
- [x] **1.7** `payment_status` capability added to `model_router._TASKS`
      (conversation tier) and to the guest and sales candidate lists.
- [x] **1.8** Prompt rules in `GROUND_RULES` and `SALES_GROUND_RULES`: a guest
      saying they paid is not the bank saying it settled.
- [x] **1.9** Tests — `tests/test_booking_status.py` (16),
      `tests/test_booking_confirmation.py` (23), and
      `scripts/payment_status_e2e.py` (29 against the real database, a real
      Razorpay order and a real webhook signature).
- [x] Migration `every_selling_agent_can_check_whether_the_money_arrived`
      grants the tool to the 14 guest/sales agents that already exist.
- [x] `tests/fake_supabase.py` grew `is_("col", "null")`, which it needed and
      did not have.

**Acceptance, met**: the transcript replays green. Before the webhook,
"payment hogyi" → `PAYMENT_PENDING` and the confirmation sentence is refused.
After it → `CONFIRMED`, reference reported, balance reported, guardrail
satisfied, run succeeded instead of escalating — and the guest was already told
unprompted, in Hinglish, thirty seconds earlier.

**Known limit, deliberately not papered over**: a payment that settles after
Meta's 24-hour Instagram window has closed cannot be announced at all — no
template mechanism exists on that channel. The guest is not stranded;
`get_booking_status` answers the moment they write again.

Suites green afterwards: 1,084 unit · sales_e2e 185 · booking_engine_e2e 65 ·
instagram_e2e 66 · ai_e2e 98 · golden_e2e 88 · payment_status_e2e 29.

## Phase 2 — Identity, session and language ✅ DONE

The agent should know who it is talking to, in what language, and where in the
funnel they are — without re-reading the transcript.

- [x] **2.1** Migration `a_guest_who_only_ever_used_instagram_is_still_a_guest`:
      `guests.instagram_id` + partial unique index on
      `(property_id, instagram_id)`. Applied.
- [x] **2.2** `_upsert_conversation` matches an Instagram guest on
      `instagram_id` instead of `ilike(phone, …)` against a 17-digit IGSID,
      which matched nothing that has ever existed. The handle is recorded on
      the guest row at fulfilment, inside `link_booking` — the one moment where
      the handle and the guest record are both in scope. A drive-by "hi" still
      creates no guest record.
- [x] **2.3** `services/messaging/session.py` — session state in
      `conversations.guest_profile`, derived from `ToolOutcome` inputs and
      outputs only. Stages `browsing → quoted → awaiting_payment → booked`.
      `describe()` puts the dates in the prompt, but only when there is no live
      quote: two descriptions of one stay is how a model quotes one and books
      the other.
- [x] **2.4** `services/messaging/language.py` — en/hi/**hinglish**, detected
      from function words rather than content words, stored on the conversation,
      merged into `guest_profile` so it cannot clobber the session. `detect()`
      returns None for "ok"/"yes"/a name, which is what stops a two-letter reply
      overwriting a real request.
- [x] **2.5** `AgentContext.language` / `.session`, both set by the pipeline;
      `prompts.build_system_prompt` adds the instruction only for a *remembered*
      preference — what the guest just wrote is in the message already.
- [x] **2.7** Tests — `tests/test_conversation_memory.py` (17), plus four new
      assertions in `scripts/instagram_e2e.py` driving the real pipeline: the
      request is remembered, the *next* turn's system prompt carries it, and
      "Ok" does not erase it.

- [ ] **2.6** History trimming — **deferred into Phase 3 with a reason.** The
      measured token cost is not the ten message bodies; it is
      `get_stay_options` returning every room type × every rate plan ×
      per-night breakdown, which is what took turns from 3k to 11k tokens.
      Trimming history first would be optimising the smaller half and losing
      continuity for it. See 3.1.

Suites green afterwards: 1,101 unit · instagram_e2e 70 · payment_status_e2e 29.

## Phase 3 — Retrieval that matches the question ✅ DONE

**The plan's hypothesis was wrong and the measurement says so.** This phase was
written assuming the expensive turns were expensive because `get_stay_options`
returned a fat matrix. It does not: measured over 119 real calls it averages
1,188 characters and peaks at 1,948.

What the cost actually is, over 1,000 real Vertex runs:

| tools called | runs | avg tokens_in | avg latency |
| --- | --- | --- | --- |
| 0 | 382 | 2,292 | 6.0 s |
| 1 | 406 | 5,253 | 9.9 s |
| 2 | 68 | 6,908 | 13.1 s |
| 3 | 43 | 10,499 | 20.7 s |
| 4 | 8 | 17,516 | 21.0 s |

The floor is 2,292 tokens for a turn that calls nothing, and the guest agent's
**23 tool schemas are 14.4 KB — about seventy per cent of the base prompt**.
Every model step re-sends all of it, so a three-tool turn pays for the catalogue
four times. The cost is the menu, not the meal.

- [x] **3.1** ~~Narrow `get_stay_options`~~ — **measured and not needed.**
      Recorded rather than deleted: the next person to assume this should find
      the number.
- [x] **3.1a** `services/ai/toolsets.py` — the classifier already decides what a
      message is about, one cheap call before the run opens, and until now that
      decision only picked a model tier. It now picks the menu as well.
      `describe_for_provider(ctx, task=…)` intersects the task's set with the
      agent's grants.

      | task | tools | schema | saving |
      | --- | --- | --- | --- |
      | guest_faq | 6 | 2,719 | **−81%** |
      | reservation_lookup | 9 | 4,594 | −68% |
      | availability | 8 | 5,655 | −61% |
      | payment_status | 10 | 6,522 | −55% |
      | booking | 11 | 8,244 | −43% |
      | guest_conversation | 23 | 14,406 | −0% (deliberate) |

      **It cannot widen a permission.** Every set is intersected with
      `agent_tools`, and narrowing changes only what is *described* — a tool the
      model somehow names anyway still goes through `permissions.check()`
      unchanged. That asymmetry is also the safety valve: a mistake in the table
      costs tokens, never correctness.

      `guest_conversation` narrows nothing on purpose. It is both the greeting
      task and the fallback for a message the classifier could not read, and
      under-equipping the turns we understand least is exactly backwards.
- [x] **3.2** `get_property_knowledge` — **already correct, verified not
      rebuilt.** Ranked keyword scoring over `keywords` and the fact's own
      question, never over the answer prose (matching the answer is how "do you
      have a helipad?" returned the check-in policy), stopword list, threshold
      of 3, capped at 4 facts, and an explicit `found: false` that tells the
      agent to say it does not know.
- [x] **3.3** Hotel semantic memory — **not built, deliberately.** 3.2 answers
      the question and can say "we have not written that down", which a
      similarity score cannot. Revisit when a hotel has enough unstructured
      material that keyword matching demonstrably misses; there is no such
      hotel today.
- [x] **3.4** Property identity is already cached with a 300 s TTL in
      `prompts._identity`. Policies stay behind a tool on purpose — a stale
      check-in time inside a prompt is worse than a tool call.
- [x] **3.5** Measured above and in `tests/test_toolsets.py`, which asserts the
      reduction rather than describing it.
- [x] **2.6** History trimming, carried forward from Phase 2 — **not needed.**
      Ten message bodies are a few hundred tokens against a 3,600-token
      catalogue. Trimming them would have cost continuity to save the smaller
      half.
- [x] Tests — `tests/test_toolsets.py` (28): the never-widens invariant over
      every task × every agent definition, every name in the table checked
      against the registry (a typo silently removes the tool it meant to
      include), handoff and knowledge always on the menu, and two runtime tests
      proving `run_turn` passes the classification through.

Suites green afterwards: 1,129 unit · ai_e2e 98 · sales_e2e 185 ·
instagram_e2e 70.

## Phase 4 — Photos and rooms worth showing

The photo tool works. The shelves are empty: zero photos across ten properties
and twenty-one room types, and two of twenty-one have a description.

- [x] **4.1** — done, and it turned out to be three missing things rather than
      one form field.

      * **There was no way to edit a room at all.** Only `GET` and `POST`
        existed on `/room-types`. Every room type in production was created
        before the form asked for a description, amenities or photos, so every
        one had none — and the only "fix" was to delete the room, which takes
        its inventory, its rate plans and its history with it. New:
        `PATCH /properties/{id}/room-types/{room_type_id}`, where an absent key
        means unchanged and an empty value means cleared. Without that
        distinction, renaming a room would silently empty its description.
      * **There was nowhere to put a file.** The form asked for photo *links*
        — "paste them from your website, or Instagram" — because StayOS had no
        image storage. Every hotel answered it with nothing. New:
        `POST /properties/{id}/media`, multipart, property-scoped prefix built
        server-side, 5 MB, JPG/PNG/WebP/AVIF, straight to the GCS bucket the
        marketplace already uses. Proven live: 201, public URL fetched back
        byte-identical, prefix correct, SVG refused 415.
      * **No screen showed what was missing.** The Rooms page was a
        housekeeping board; room types appeared only as a lookup. New
        `RoomTypesPanel` — a card per room type with its cover photo, and where
        a room is incomplete, a sentence rather than a progress bar: *"Your AI
        can't tell guests about photos or a description yet."* Editing is
        inline, on the screen an owner already opens.
      * `PhotoPicker` uploads as each file is chosen, not on submit: the
        slowest part of the form should not run at the moment the owner thinks
        they are finished, on a connection that may not survive it.

      **Deployment change, recorded in `DEPLOYING_STAYOS.md`:** `GCS_BUCKET_NAME`
      was on that document's *"must NOT be set"* list — correct until StayOS
      grew an uploader. It is now required, and its absence answers 503 with a
      readable sentence and a log line naming the variable, rather than a 500
      from inside the GCS client. ⚠ **Still to do on the production
      environment**: add `GCS_BUCKET_NAME=travbuddy-media-2` in Coolify.

- [ ] **4.2** AI room import: brochure/PDF/photos/website URL → Gemini
      extraction → **draft** room types the owner reviews and approves. Reuses
      `services/vertex.py`; writes through `onboarding.add_room_type` /
      `update_room_type`. No second extraction pipeline, no direct writes.
- [ ] **4.3** Conversational correction of an extracted draft ("isme breakfast
      included nahi hai") mapping to the structured field.
- [ ] **4.4** Instagram media send: attach room photos as real IG attachments
      where the API allows; fall back to the room page link.
- [ ] **4.5** Completeness score per property, extending the per-room gaps the
      panel already shows.

Tests: `tests/test_room_type_edit.py` (18) on the PATCH contract and its
isolation, plus a new section in `scripts/onboarding_e2e.py` (65 total) proving
it over real HTTP with a real token and real RLS — including that another
hotel's room cannot be edited or written to through its path.

Suites green afterwards: 1,147 unit · onboarding_e2e 65 · golden_e2e 88.

## Phase 5 — Hotel onboarding, AI-assisted

- [ ] **5.1** Extend 4.2 to the whole property: name/address/description/
      amenities/policies/meals from a brochure or an existing website.
- [ ] **5.2** Geocode on create (fills the null `lat`/`lon`) — needed by Phase 7.
- [ ] **5.3** Missing-information prompts driven by the completeness score.
- [ ] **5.4** Owner approval gate before anything becomes agent-visible.

## Phase 6 — Operator view and confirmation surfaces

- [ ] **6.1** Booking page: tokenised, non-sequential URL
      (`/b/{token}`) showing reference, hotel, dates, nights, rooms, guests,
      room type, rate plan, the full price breakdown, paid/balance, cancellation
      policy and status. Token stored on the reservation, not derived from the ref.
- [ ] **6.2** Receipt on the same page.
- [ ] **6.3** Inbox: a session panel per conversation (guest, language, stage,
      dates, quote, payment, booking) so staff do not read the transcript to
      learn the state. Transcript stays for debugging.
- [ ] **6.4** Instagram → website handoff preserves the session (dates, guests,
      room) so nothing is re-entered.

## Phase 7 — Hotel websites that search and answer engines can read

The largest single piece of new work. Today `/stay/:slug` is invisible to search.

- [ ] **7.1** Decide the serving model: server-render hotel pages (Next.js route
      in `Travbuddy_website`, or a small Next app fronting `partner.travbuddy.in`)
      versus prerendering the Vite build. **Recommendation: Next.js**, because
      custom domains, `generateMetadata`, JSON-LD and ISR all fall out of it and
      the consumer site already does exactly this for `/hotels/[slug]`.
- [ ] **7.2** Content-rich hotel page from existing Supabase data: overview,
      rooms, amenities, dining, location, policies, FAQ, booking CTA — real
      `h1/h2/h3`, server-rendered.
- [ ] **7.3** `generateMetadata` per hotel; canonical that ignores
      `?checkin=/?guests=/utm_*`.
- [ ] **7.4** JSON-LD: `Hotel`/`LodgingBusiness`, `HotelRoom`, `Offer`,
      `PostalAddress`, `GeoCoordinates`, `BreadcrumbList`, `FAQPage`.
      No `AggregateRating`/`Review` until real reviews exist.
- [ ] **7.5** FAQ generated *only* from verified fields; unknown → omitted.
- [ ] **7.6** Room pages `/…/rooms/[slug]` where a room has enough unique content.
- [ ] **7.7** Indexability gate: a completeness threshold decides
      `index` vs `noindex`. Thin profiles stay out of the index.
- [ ] **7.8** Sitemaps (hotels, rooms), regenerated on publish; IndexNow ping
      reusing the pattern in `lib/seo/indexnow.ts` on the consumer site.
- [ ] **7.9** Core Web Vitals: responsive image variants, hero preload, lazy
      everything else.
- [ ] **7.10** SEO readiness panel in the operator UI — plain language, no jargon.

## Phase 8 — Security and hardening

- [ ] **8.1** `razorpay_webhook_events` RLS (present the SQL, do not auto-apply).
- [ ] **8.2** Booking/receipt tokens: unguessable, revocable, no sequential ids.
- [ ] **8.3** Re-verify hotel isolation on every new tool and query added above.
- [ ] **8.4** Confirm the new guest-identity path cannot join two guests across
      properties.

## Phase 9 — Evidence

- [ ] **9.1** Replay the reference transcript end to end against the new build.
- [ ] **9.2** Record before/after tokens, latency and model mix in this file.
- [ ] **9.3** Full suite green (currently 1,045 unit + 19 E2E).

---

## Decisions taken

- **No new booking, payment, memory or pricing system.** Everything below hangs
  off `quotes` / `inventory_holds` / `payment_orders` / `reservations` /
  `property_knowledge`, which are already the source of truth.
- **Session state goes in `conversations.guest_profile`**, an existing unused
  column, not a new table. It is a cache of tool outputs, never of model prose.
- **The lifecycle enum stays split** across `quotes.status`,
  `payment_orders.status` and `reservations.{status,payment_status}`. A single
  new enum would be a fourth opinion about the same facts. `get_booking_status`
  is the one place that composes them into the vocabulary the brief asks for.
- **Backend decides booking status; the model reports it.** Unchanged, and
  Phase 1.5 keeps the guardrail honest rather than loosening it.
