# StayOS — risk register and production-readiness gates

*Written 2026-08-25 for the final hardening pass. Mission 1 asks for a written
risk register before fixing; this is it, with what happened to each entry.*

Severity as the brief defines it. **P0** can cause financial, booking, inventory,
security or guest-trust damage. **P1** is a major correctness or reliability
problem. **P2** is performance, UX or maintainability.

---

## What was found, and what happened to it

| # | Sev | Risk | Evidence | State |
|---|-----|------|----------|-------|
| 1 | P0 | Another hotel's rows reachable by id | Never tested. Every guest tool mentions `property_id`, which is not proof | **Closed.** 17 tests attempt it across ten tools and the control proves the attempts are real |
| 2 | P0 | A guest's claim becoming business truth | `_Facts.set` accepted any field at `GUEST_SAID`, the top rank | **Closed.** `BUSINESS_AUTHORITY` — only a verified tool *result* may set payment, stage, quote, reservation |
| 3 | P0 | Guest told a person is coming when nobody was asked | Two paths: the model writing it (happened, failed the gate), and `_ensure_human` swallowing its own failure | **Closed.** The promise creates the handoff; if it cannot be recorded the promise is withdrawn |
| 4 | P0 | Inventory drift invisible until a guest arrives | Reconciliation existed; nothing ran it. Found real drift twice, both times by hand | **Closed for detection.** Hourly beat task, reports and never corrects. **18 units still oversold at Casa Mandala** — production rows, not touched |
| 5 | P1 | `"room 204 mein AC nahi chal raha"` → group pricing for 204 rooms | Reproduced | **Closed.** `room_number` is its own entity; `"204 wala room"` too |
| 6 | P1 | `"thanks"` loading all 38 tool schemas | 7,607 tokens measured on one of the commonest turns | **Closed.** 823 tokens. `"ok"` deliberately excluded — it can mean *yes, book it* |
| 7 | P1 | Test double cannot express what production queries | `or_` and `lt` were missing and made whole paths untestable — twice | **Bounded.** Every operator the StayOS guest path uses is now expressible. `maybe_single` (51 sites) is marketplace-only |
| 8 | P2 | Beat guards assumed one task prefix | My own new task tripped three of them | **Closed.** Both StayOS prefixes accepted; the task is routed |

---

## Where the model could invent business truth

Audited tool by tool. Every figure the model can see now arrives beside the
words for it — `money.with_display` at `registry.execute` — and one mechanical
property holds the rest: *a tool result must never contain a percentage and a
total without their product.*

The model may still **say** a percentage (`"8% off for three rooms or more"` is
true and safe). It may not produce their product, and the guardrail refuses one
that no tool returned.

---

## Where a fallback can hide an error

| Fallback | Named? | Observable? |
|---|---|---|
| Negotiation rules, DB read fails | `emergency` | on the run row |
| Negotiation settings, read fails | `emergency` | on the run row |
| Group tiers, read fails | no | log only |
| Group policy, read fails | no | log only |
| Room list, read fails | no | log only, resolver goes silent |
| Idempotency lookup fails | no | log only |
| Model returns nothing | classified | `recovery` on the run row |

| Redis unavailable for the conversation lock | no | log only |

The Redis one is worth stating in full because it is the only fallback that
changes what a *guest* sees. Losing the lock means two workers may answer the
same conversation, and `_deliver` keys its idempotency on the run id — which
differs per run — so the guest can receive the same answer twice. What cannot
happen is a double booking or a double charge: the message-scoped idempotency
key refuses the second mutating call, and inventory is held in Postgres. A
duplicated sentence is embarrassing; the two failures underneath it are not
available.

The last four are **accepted P2**: each degrades to the behaviour before the
feature existed, and none grants more authority. The two that could grant more
authority are the two that are named.

---

## Production-readiness gates

| # | Gate | State |
|---|------|-------|
| 1 | All P0 closed | **Partial** — 4 is closed for detection; the 18 oversold units need one command |
| 2 | P1 closed or accepted | Yes |
| 3 | Production entrypoints covered | Yes — `process_conversation` has 16 tests |
| 4 | Mutation tests passing | Yes — 37/37 |
| 5 | Multi-turn tests passing | Yes — 83 scenarios, 171 turns, longest 20 |
| 6 | Cross-tenant tests passing | Yes — 17 |
| 7 | Inventory reconciliation clean | **No** — Casa Mandala |
| 8 | Approval race tests | Yes |
| 9 | Payment idempotency | Yes |
| 10 | Cancellation idempotency | Yes |
| 11 | Escalation worker proven | Yes — task body executed, not just scheduled |
| 12 | Model failure recovery proven | Yes — 13 classes, ladder tested |
| 13 | Guardrail integrity | Yes — `guardrails.py` unchanged this pass and last |
| 14 | No silent fallback without telemetry | **Partial** — see the table above |
| 15 | No channel/tool mismatch | Yes — 600 tests across six channels |
| 16 | No dangerous business-truth inference | Yes |
| 17 | Realistic hotel corpus | **One hotel.** Casa Mandala, still |
| 18 | 100+ conversation evaluation corpus | **No** — 83 scripted scenarios, 44 live turns per model |
| 19 | p50/p95 recorded | Yes, from evaluation not production |
| 20 | Cost per conversation | Partial — tokens per turn measured, not per conversation |

**Gates 1, 7, 14, 17 and 18 are not met.** The system is not production-ready by
its own definition, and the report says so.

---

## What is UNKNOWN — REQUIRES POST-DEPLOY OBSERVATION

- The rate at which `emergency` rule fallback fires. Queryable by construction; never observed.
- The rate at which the recovery ladder fires, and how far it climbs.
- Whether the room resolver's threshold holds against room names other than Casa Mandala's four.
- Whether the redundant-call fix moved the real `search_availability → get_stay_options` pair count.
- Guardrail block rate after the upstream fixes.
- Real p50/p95 under production concurrency rather than a serial evaluation.
