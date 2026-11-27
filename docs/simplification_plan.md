# Simplification Plan — Copilot Off, One-Pass Multi-Stop, Structured Actions

*Written 2026-07-16. Status: **SUPERSEDED** by `docs/trip_engine_architecture.md`
(v4, same day) — the priority shifted from Vertex-call reduction to a
deterministic, spec-driven, production-grade engine. Key deltas: ONE unified
generation pipeline for 0..N stops (no composer/supervisor split), an immutable
versioned Trip Spec as the source of truth, graph v2 holding only `result_id`
references (never raw payloads), store-first writes + atomic generation
commits, exact ID hydration on read, and an M0–M6 migration roadmap. The
component analysis and PR-level detail below remain useful reference; follow
the v4 doc for ordering and design decisions.*

Product/architecture decision (Sahil): the conversational copilot is too expensive
(Vertex free-trial quota) and too non-deterministic for the current stage. Replace
it with **structured trip creation** (stops chosen before generation, one pass) and
**structured post-generation actions** (add stop, change dates, …) — no AI chat.
Keep moving toward the clean separation: **Trip Graph** (structure) / **Search
Store** (provider raw data) / **Booking objects** (provider payloads). The graph
describes the trip; it never owns provider payloads.

Key discovery shaping this plan: the structured-action machinery **already
exists** — `EditAction` (`app/api/v1/trip_state/endpoint.py:39`) bypasses the LLM
and routes straight to deterministic builder handlers (`select_leg_option`,
`select_stop_hotel`, …). Items 4–5 below extend that pattern rather than building
a new system.

---

## PR 1 — Commit the already-verified fixes (DONE, uncommitted on `fix/bugs_1`)

Files: `app/services/vertex.py`, `app/api/v1/plan_trip/journey.py`,
`app/trip_state/graph.py`, `app/trip_state/retrieval.py`,
`app/api/v1/trip_state/endpoint.py`.

1. **Refresh-transport fix** — read path re-inlines `flights/trains/buses` buckets
   (`_project_journey_block(..., include_buckets)`; `projected_state` defaults
   True) while persisted patches stay skinny (`project()` default False). Thin
   persisted graphs get option raws back from the search store at read time via
   `retrieval.hydrate_graph_option_raws` (scope/city batch match + mode/operator/
   clock/fare option match, ≥2 signals). Verified on prod trips `b997937c` (0→16
   items) and `756ebf34` (0→10).
2. **Vertex region cooldown** — a 429/503 region cools 90 s (Retry-After honored,
   cap 300 s); every call starts in a healthy region (`_healthy_first`).
3. **Deterministic route-plan cache** — `_gemini_plan_route` is temp-0 + seeded on
   (origin, dest, budget, travelers, modes, date), so identical plans are served
   from an in-process TTL cache (24 h, 512 entries, deepcopy both ways).

## PR 2 — Copilot behind a feature flag

**Backend** (small):
- `config.py`: `copilot_enabled: bool = False` (env `COPILOT_ENABLED`).
- `endpoint.py` `/edit` + `/edit/stream`: free-text `message` (LLM agent path)
  → 403 when flag off. **Requests carrying `action` pass through unchanged** —
  the workspace select clicks use these same endpoints.
- `GET /{trip_id}/chat`: return `{messages: []}` when off (frontend loads it on
  mount — degrade, don't 403).
- `agent.py` / `copilot.py` / `builder.py`: untouched. Graph/builder machinery
  stays intact for PR 3/4 and future re-enable.

**Frontend** (safar_website):
- `components/workspace/workspace-client.tsx:23,327` — gate `<AgentConsole/>`
  behind `NEXT_PUBLIC_COPILOT_ENABLED`.
- `lib/use-trip-workspace.ts` — keep `sendEdit` (~line 1006; carries structured
  actions), skip the chat-transcript fetch (~line 794) when flagged off.
- `agent-console.tsx`, `chat-cards.tsx` remain in tree, unrendered.

## PR 3 — One-pass multi-stop generation

**Request shape** — extend v2 `PlanRequest`:

```json
{ "origin": "Bhiwani", "destination": "Udaipur",
  "stops": [{"city": "Jaipur", "nights": 2}, {"city": "Pushkar", "nights": 1}],
  "days": 6, "budget": "mid", "travelers": 2 }
```

Validation: max ~4 stops, distinct cities, `sum(nights) + terminal nights ≤ days`
(auto-allocate nights when omitted).

**Key refactor** — extract the pure core from `builder.handle_add_leg`
(`builder.py:492`), which today mixes route search, graph mutation, date
resequencing and chat-reply text:
- `build_leg(graph, from_node, to_node, date, …) -> TripLeg` — search + route
  construction only (reuses `_shaped_journey`, gateway discovery via
  `find_gateways`, sold-out next-day probing).
- `handle_add_leg` becomes a thin chat wrapper over it (copilot still works when
  re-enabled).

**New composer** `app/trip_state/compose.py` (~200 lines orchestration):
1. Build nodes origin → stops → terminal with nights; `_resequence_dates` once.
2. `build_leg` per edge **in parallel** (N+1 outbound + 1 return; return mirrored
   from the outbound where multi-hop, as today).
3. `run_plan_trip` itinerary per stayed stop, in parallel.
4. One `_geocode_nodes`, one `_graph_patches`, one `sync_trip_to_store`.
5. Emit via existing `_Emitter` with per-leg/per-stop phases → streaming progress
   UX preserved.

`generator._generate`: `stops` present → composer; `stops` empty → existing
`supervisor_run` path **unchanged** (zero risk to the main flow). Migrating
single-destination onto the composer is a later cleanup once proven.

**Unlocked by the composer:** batches scoped by real `leg-N`/`stop-N` ids from
birth (kills the `going`/`return` vs `leg-N` scope duality), and each graph
option stamped with its `search_results.id` at write time — the typed
Graph↔Store reference (item 6). The fuzzy-matching hydration then becomes a
legacy-trip-only fallback.

**Frontend:** plan-creation form gets an "Add stops" chip list (existing
autocomplete) + nights stepper. Workspace read side already renders multi-city
(`journey-board` onward segments, `stop-stays`, `route-timeline`) — no changes.

## PR 4 — Structured trip actions (no chat)

Extend `EditAction` with structural types, implemented as **spec-driven
rebuilds**, not incremental graph surgery:

| Action | Implementation |
|---|---|
| `add_stop` / `remove_stop` | mutate `request.stops`, re-run composer |
| `change_dates` | mutate `request.departure_date`, re-run composer |
| `change_budget` / `change_style` | update request; re-rank existing options (no re-scrape) + re-run itinerary calls only |
| `regenerate` | re-run composer as-is |
| `select_*` | already shipped, untouched |

Why rebuild over surgery: the incremental edit path is where the **known
return-edge-empty bug** lives (`_repoint_return_leg`/`_sync_legs_after_change`
can leave a return edge with `routes: []` after a re-point — observed on prod
trip `b997937c`, store scope `return` holds 6 trains the edge lost). A
deterministic rebuild from spec is one code path, idempotent, self-healing; the
route-plan cache + the store's `params_hash`/`find_live` 24 h reuse make
rebuilds cheap. Add a per-trip regenerate rate limit (~5/hour).

## PR 5 — Demote the recommendation LLM to muscle tier

`recommendation_agent.py` (`recommend_flights`/`recommend_hotels`) runs on
**brain** and fires up to 2 extra calls per journey search, only to write the
advisory "why this pick" text. Move to **muscle** (flash-lite, separate quota
pool) — one-line tier change, roughly halves brain RPM per generation.

---

## Vertex call estimate (from actual call sites)

| Flow | Today (brain calls) | After |
|---|---|---|
| Single-dest generation | ~6 (2 route plans + 3–4 reco + 1 itinerary) | ~3 brain + 3 muscle (reco demoted, plans often cached) |
| Copilot edit ("add Jaipur") | 9–13 (agent turns + re-plans + reco + itinerary) | **0** — structured actions use no agent loop |
| 2-stop trip, typical session | ~25–30 across gen + 2 edits | **~6–8, once** (3–4 plans, mostly cached + per-stop itineraries) |

Net ≈ **70–75% fewer brain-tier calls**; region cooldown removes the wasted-429
retries on the remainder. Infra: edit sessions were also the scraper-heavy path
(every agent re-plan re-scraped flights/trains/hotels) — structured selections
scrape nothing, rebuilds reuse live batches via `find_live`. DB requests drop:
one composer write-cycle vs. streams of per-edit patch events.

## Edge cases & regressions to watch

1. **Don't gate `action` requests** — selects share the copilot endpoints;
   gating them breaks workspace picks.
2. **No-service legs** (e.g. BAO→UDR) mid-chain: composer must mark the leg
   `no_route` + attach gateway alternatives without failing the generation.
3. **Old trips** with mixed `going`/`leg-N` scopes must keep rendering — keep the
   hydration fallback.
4. **Generation wall-clock**: a 3-stop pass runs ~8–10 parallel searches — check
   the resilience layer (global queue, breakers) for the higher per-trip
   concurrency; SSE stream timeout must cover the longer pass.
5. **Nights arithmetic**: `_resequence_dates` must reconcile `days` vs
   `sum(nights)`; an off-by-one silently shifts the return date.
6. **Rebuild wipes selections** (`leg.selection`): carry picks forward by option
   identity where possible, else reset explicitly and say so in the UI.
7. **`/chat` on mount** must degrade gracefully (empty, not error) when off.

## Recommended order

PR1 (commit, today) → PR2 (flag, tiny) → PR3 (main piece) → PR4 (on top) → PR5
(anytime). Design choices to hold firm: extract `build_leg` rather than
duplicating search logic in the composer; spec-driven rebuilds for structural
actions rather than reusing the incremental edit path (that's where the known
bugs live).
