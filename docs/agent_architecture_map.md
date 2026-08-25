# StayOS agent — the architecture, and where it can still go wrong

*Written 2026-08-25, for the deep reliability pass. §1 of the brief asks for
this before anything is changed; the findings at the end are what changing
things then produced.*

The one-sentence version: **the LLM proposes, the application decides, and every
number a guest is told came from a tool.** Everything below is either an
enforcement of that or an admission of where it is not yet enforced.

---

## 1. The path a guest message takes

```
   WhatsApp / Instagram / website
        │  signed webhook, verified in the request
        ▼
   api/v1/messaging/webhooks          persist first, acknowledge fast
        │                              conversation_messages UNIQUE(channel, external_id)
        │                              ← this index is the real idempotency
        ▼
   messaging/pipeline.dispatch  ──►  Celery  ──►  messaging_tasks
        │                                          conversation_lock (Redis, 90s)
        ▼
   messaging/pipeline.process_conversation
        │  agent_may_answer?          human takeover stops the AI, before a run exists
        │  ai_routing.route           which agent, from channel + property config
        │  billing.allowed            the credit gate, fails open
        │  language.detect / remember the alphabet to answer in
        │  _pending                   inbound messages with agent_run_id is null
        ▼
   ai/runtime.run_turn
        │
        ├─ classify.classify           task → tier. Greetings skip it entirely.
        ├─ model_router.select         task → model, thinking budget, turn budget
        ├─ prompts.build_system_prompt property identity, session, policies
        ├─ registry.describe_for_provider
        │       └─ toolsets.for_task   the menu, never the door
        ├─ required_actions.detect     ★ deterministic, no model, guest turns only
        │
        ├─ ┌── the model loop, ≤ agent_max_tool_turns ──────────────┐
        │   │  provider.step  → speak, or call tools                │
        │   │  dedup: called_reads / called_writes (this turn only) │
        │   │  registry.execute ─────────────────────────────────┐  │
        │   │      permission.check                              │  │
        │   │      input_model validate                          │  │
        │   │      spec.precheck        business rules           │  │
        │   │      autonomy.decide      execute / approve / refuse│ │
        │   │      spec.revalidate      approval path only       │  │
        │   │      ★ idempotency guard  one message, one effect  │  │
        │   │      spec.fn              the tool body            │  │
        │   │      spec.verify          read it back             │  │
        │   │  └──────────────────────────────────────────────────┘  │
        │   │  agent_actions row, every time, including refusals    │
        │   └────────────────────────────────────────────────────────┘
        │
        ├─ ★ the forcing pass          requirements the model did not meet
        │       └─ registry.execute    the same door. Nothing bypasses a gate.
        │
        ├─ script.normalise            Devanagari → Latin for a Hinglish guest
        ├─ guardrails.check_reply      ★ every reply, including handoffs
        └─ agent_runs closed           status, tokens, latency, requirements
        ▼
   pipeline: _mark_answered → session.merge → deliver → _ensure_human
```

★ marks a layer that exists because the model, at some point, did something a
hotel could not afford.

---

## 2. Sources of truth

Nothing on this list is ever derived from model output.

| Truth | Owner | How it is protected |
|---|---|---|
| Availability | `room_inventory` + `hold_inventory()` | one atomic statement per night; proved under 20-way concurrency by `scripts/concurrency_probe.py` |
| Price | `pms/pricing.price_stay` | returns display strings beside paise so nothing needs converting |
| Discount floor | `pms/negotiation` + `agent_configs.max_discount_pct` | the engine answers; the model relays |
| Deposit | `pms/pricing.token_paise`, `policies.deposit_for` | returned on every quote that has one — see finding 6 |
| Booking state | `reservations` + `convert_hold()` | a reservation exists only after stock moved |
| Payment state | Razorpay webhook → `_fulfil_booking` | signed; the agent may never assert payment |
| Refund | `pms/cancellation` | computed from policy, never quoted by the model |
| Permission | `agent_permissions`, `agent_tools` | absence is denial |
| Autonomy | `agents.autonomy_level` | a rung, not a prompt line |
| What was said | `conversation_messages` | stamped with the action that sent it |

### Derived, and never persisted as truth

`conversations.guest_profile` (the session) is **advisory**. It is written from
tool outputs, not from prose, and nothing reads it to decide a price, a room or
a payment. It exists so the agent stops asking a guest things they have already
said. `messaging/session.py` carries the staleness and supersession rules.

---

## 3. What the model decides, and what it cannot

**The model decides:** what the guest means, which allowed action to propose,
what to say, and in which language.

**The model cannot decide:** whether an action runs (`registry.execute`),
whether the conversation ends (`arbiter`), what anything costs (`money.display`
and the pricing engine), whether a stay exists (`guardrails`), or whether a tool
it cannot see exists (`registry.available_for`).

Three layers make that true rather than customary:

1. **`registry.execute` is the single door.** Tool bodies contain no security
   logic, so a tool author cannot forget it. Adding a gate here covers every
   tool that will ever exist.
2. **`required_actions`** decides *that* a business tool runs, deterministically
   and before the model is consulted. It never decides what may be said.
3. **`arbiter`** decides whether a handoff the model asked for is supported by
   the conversation it claims to describe.

---

## 4. Where state can go wrong, and what holds it

| Risk | Held by | Proven by |
|---|---|---|
| Two guests, one room | `hold_inventory` ON CONFLICT … WHERE | `scripts/concurrency_probe.py`, 20 callers / 3 units |
| A stale availability read | the same statement — the guard is at write time | same probe, second race |
| One message answered twice | `conversation_messages` unique index, then `_mark_answered`, then the idempotency key | `tests/test_inbound_race.py`, `tests/test_turn_idempotency.py` |
| Two workers on one conversation | `conversation_lock` (Redis, 90s) + task retry to 120s | `scripts/celery_ordering_e2e.py` |
| Messages answered out of order | `_pending` orders by `sent_at`, then `created_at` | the tie is real: 17 of 59 batches |
| A tool run twice in one turn | `called_writes`, never reopened | `tests/test_ai_runtime.py` |
| A read cached past a write | `called_reads.clear()` on every successful mutation | same |
| Inventory drifting from reservations | `scripts/reconcile_inventory.py` | run after every e2e suite |
| A fixture eating real stock | `tests/test_inventory_hygiene.py`, three source-level rules | the 377 room-nights |

---

## 5. Where a failure is swallowed, deliberately

Each of these is a decision, not an oversight. They are listed because "where
are failures swallowed" is a question this map has to answer.

| Swallowed | Why | Consequence if it fires |
|---|---|---|
| `_write_action` insert | an audit failure must not take the guest's answer with it | a hole in the trace; logged at `exception` |
| `usage.record_run` | a meter that can end a conversation is worse than a gap in the meter | under-billing; logged |
| `link_result` / `_stamp_message_action` | provenance is not worth a 500 | the trace does not join up |
| `billing.allowed` on error | a Supabase outage must not read as "you have not paid" | a hotel briefly over its limit |
| `_deliver` exceptions | the conversation goes to a person instead | a guest waits for a human |
| the idempotency lookup | a guard is not the guest's answer | the behaviour before the guard existed |
| Google connection read | a deployment without the table is not a reason to refuse every link | fewer trusted hosts |

---

## 6. The six channels, and what each can do

`registry.channel_capabilities` derives this from the messaging policy rather
than from a hand-kept list — the previous list held `send_message` alone while
`send_template` and `send_room_photos` also reach `messaging.send`, so both were
offered on channels where they raise.

| Channel | outbound | templates | notes |
|---|---|---|---|
| whatsapp | yes | yes | 24-hour window; `last_inbound_at` decides it |
| instagram | yes | no | no template mechanism |
| website | no | no | replies are the HTTP response |
| internal | no | no | the staff console |
| phone | no | no | |
| email | no | no | |

`tests/test_channel_capability.py` derives the same answer from the tool bodies
and fails if a declaration is missing.

---

## 7. Findings from this pass

Every one of these was found by measurement or by the live model, not by
reading. Each has a regression test.

1. **The rule against lying about a booking was refusing the truth about one.**
   Every honest answer to "is my booking confirmed?" contains the words *booking
   confirm*. Four honest sentences — including one that *denies* a booking
   exists — were refused, and a refusal is a handoff. Replaced with grammatical
   mood: `CLAIMED` needs a tool that secures a stay, `DENIED` needs a lookup that
   came back unsecured, `HYPOTHETICAL` needs nothing.

2. **Answering the same guest message twice did the work twice.** `_mark_answered`
   is a database write inside a task declared `acks_late` with
   `autoretry_for=(Exception,)`. It cannot be made unfailable — swallowing its
   error would bring the message straight back from `_pending` — so the mutating
   tools underneath it are now idempotent within the message being answered.

3. **"human se baat karni hai" was not recognised as asking for a person.** The
   brief's own example. A paraphrase sweep of 274 phrasings found eleven misses
   and two false positives, including a year read as a budget and a room number
   read as a price.

4. **A fixture teardown scoped to one run leaked a reservation every crash.**
   Twelve units oversold at Casa Mandala, invisible to `units_booked` because
   the rows never called `convert_hold`.

5. **"Hi" is 60% of the traffic and paid for nineteen ways to price a stay.**
   `guest_conversation` is the fallback *and* the greeting task; only the first
   needs the full catalogue. 7,270 tokens per step → 672.

6. **The deposit fix from the day before had a blind spot.** Guarded on the
   booking being a group, so a two-room quote at a property with a 20% token
   left the model to multiply. `policies.deposit_for` — written for exactly this
   — had no callers at all.

---

## 8. What is still weak

Stated plainly, with no fix attached, because a known weakness written down is
worth more than one quietly carried.

**The catalogue is re-sent on every model step.** Measured over three days, 3.x
costs 2–3× the input tokens of 2.5 on the same task at the same property, and
the ratio tracks the number of tools it calls almost exactly. It is not more
verbose; it does more work, and each extra tool call is another step that
re-sends every schema. More narrowing has diminishing returns — the structural
fix is provider-side context caching.

**Compensation, not transactions.** Supabase's REST interface gives no
cross-statement transaction, so `pms/booking` undoes its own stock moves by
hand on every failure path. It is correct today and it is correct by review
rather than by construction. A hold taken by a process that is then killed is
released by its TTL, not by a rollback.

**The session is advisory and one merge deep.** `session.merge` is a
read-modify-write on a jsonb column under the conversation lock. Correct while
one worker owns a conversation; there is no optimistic concurrency behind it if
that ever stops being true.

**Imported reservations do not claim inventory.** Reservations written directly
— by a PMS import, or by a fixture — never move `units_booked`. The ledger then
under-counts and the hotel can oversell. `reconcile_inventory` finds it; nothing
prevents it.

**The arbiter is not deployed.** It exists, it is tested, and it fires in local
replays against the production database. `origin/main` does not have it.

**One property, one shape of hotel.** Almost every live measurement in this map
is Casa Mandala. Hotels with no group tiers, no token percentage, zero
inventory or a missing messaging policy are covered by tests and by
`test_launch_hardening.py`, but not by traffic.
