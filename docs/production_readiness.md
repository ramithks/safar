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
| 4 | P0 | Inventory drift invisible until a guest arrives | Reconciliation existed; nothing ran it. Found real drift twice, both times by hand | **Closed.** Hourly beat task, reports and never corrects. The 18 (really 24) oversold units are repaired — see 13 and 14. **All ten active properties reconcile to zero** |
| 5 | P1 | `"room 204 mein AC nahi chal raha"` → group pricing for 204 rooms | Reproduced | **Closed.** `room_number` is its own entity; `"204 wala room"` too |
| 6 | P1 | `"thanks"` loading all 38 tool schemas | 7,607 tokens measured on one of the commonest turns | **Closed.** 823 tokens. `"ok"` deliberately excluded — it can mean *yes, book it* |
| 7 | P1 | Test double cannot express what production queries | `or_` and `lt` were missing and made whole paths untestable — twice | **Bounded.** Every operator the StayOS guest path uses is now expressible. `maybe_single` (51 sites) is marketplace-only |
| 8 | P2 | Beat guards assumed one task prefix | My own new task tripped three of them | **Closed.** Both StayOS prefixes accepted; the task is routed |
| 9 | P1 | A guardrail block keeps no record of what it refused | 0 of 30 blocks retain the reply text. §17 asks *why did StayOS say this?* and for the largest failure class on product traffic there is no answer without a log search on a run id nobody has | **Closed.** The refused reply rides the internal telemetry channel into `agent_runs.required_actions`; never in model context, never sent |
| 10 | P1 | The evaluation marked a model unsafe by a rule the product does not enforce | Gemini 2.5 failed the promotion gate on `leaked_ids = 2`; both were payment links, which the guardrail deliberately allows by masking URLs first | **Closed.** The scorer calls `_internal_leak` rather than re-walking the pattern list; a test asserts the two never disagree on any reply |
| 11 | P1 | A quarter of the 3.x column was not 3.x | 11 of 44 turns finished on the fallback tier after two empty completions, all stamped with the configured model | **Closed.** `RunResult.served_by` carries every model the turn touched |
| 12 | P1 | `"checkout kitne baje hai?"` read as a request for a price | The matrix's *control* turn required a business action on 4/4 runs on **both** model families — the signature of a system bug charged to a model | **Closed.** The clock phrase is stripped before price words are looked for, so a message asking both still asks a price |
| 13 | **P0** | Oversold nights invisible when the ledger has no row | `disagreements` walked `room_inventory`, so a night with no row could not disagree with the table. 2026-09-21 was oversold and unreported | **Closed.** Unledgered nights are synthesised into the same shape every caller handles. The real count was **24, not 18** |
| 14 | **P0** | The repair for an oversold night depends on who is on it | Six real guests and six unswept fixtures produce identical arithmetic and opposite repairs | **Closed.** `repair_inventory.py` classifies on evidence; UNKNOWN is terminal; the repair is a cancellation with a reason, never a DELETE, and never touches a counter the rows never moved |
| 15 | **P0** | Informational questions triggering business actions | The matrix's control turn required an action on 4/4 runs on **both** model families | **Closed.** `services/ai/intent.py` — six speech-act categories gating what a turn may owe. Four more misroutes found by it |
| 16 | P1 | A headcount inferred into a room count | "5 log breakfast ke liye aayenge" booked three rooms | **Closed.** Two detectors shared the inference; the guard is shared with them |
| 17 | P1 | "deluxe" sold a room where two rooms are deluxe | A live property has `deluxe room` and `deluxe king bed`; "room" is a stop word | **Closed.** A name whose significant tokens are a strict subset of another's is ambiguous |
| 18 | P1 | A quote surviving a fact it was priced without | A blank being filled was never a correction, even when something already derived from it | **Closed.** Found by a 20-journey replay and again by an independent reconstruction |
| 19 | P1 | A gate finer than its sample could resolve | 2% ceiling on n=43, where one failure is 2.3% | **Closed.** `services/ai/gates.py` — ZERO criteria need no sample, RATE criteria carry `min_n`, thresholds unchanged |
| 20 | P1 | The evaluator's money reader narrower than the product's | "12,800 rupees" was not a figure; no decimals; no floor | **Closed.** Reads through `guardrails._amounts_in`; a differential test pins them together |
| 21 | P2 | Informational turns paying a classify call | ~2.3s and its tokens, on the commonest message class after greetings | **Closed.** `intent.category` routes them deterministically to `guest_faq` |

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
- **Whether Gemini 3.x's empty-completion rate holds at production volume.** It needed the
  fallback tier on 11 of 44 evaluation turns against 3 of 44 for Gemini 2.5 — roughly
  3.5×, on 44 turns each. Guest-visible outcome was nothing: every one recovered and no
  turn failed. But the ladder is the whole of what stands between that rate and a silent
  turn, and 44 turns cannot tell you whether the rate is 25% or noise.
- Whether the promotion gate's `invalid_handoff_rate ≤ 2%` is measurable at all at the
  sample sizes actually run. At n=43 the smallest non-zero value the metric can take is
  2.3%, so the gate is not a 2% threshold on that sweep — it is a demand for zero. Gemini
  3.x failed it on exactly one turn, an adversarial jailbreak on which the arbiter refused
  a handoff *correctly*. Either the gate needs a sample that can resolve it, or the metric
  needs to stop counting the arbiter's refusals against the model on adversarial turns.
