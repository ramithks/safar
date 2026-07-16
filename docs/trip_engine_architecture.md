# Trip Engine Architecture v4 — Deterministic, Spec-Driven, Production-Grade

*Written 2026-07-16. Supersedes the roadmap in `docs/simplification_plan.md`.
Status: for review — no code yet.*

## 0. Goal and non-goals

**Goal:** every trip is reproducible from persistent storage. A refresh, a
server restart, an interrupted SSE stream, or reopening the trip a week later
always yields the same complete state. The backend is the single source of
truth; the frontend is a pure renderer of `GET /api/v2/plan/{id}`.

**Non-goals (for now):** conversational editing (flagged off), semantic
memory/embeddings, GCS raw offload, multi-region storage.

**Design priorities, in order:** determinism → fault tolerance →
maintainability → scalability → cost. (Vertex-call reduction falls out of the
design; it is no longer the driver.)

---

## 1. Architecture

```
                ┌────────────────────────────────────────────────┐
                │  TRIP SPEC  (immutable, versioned)             │
                │  origin, destination, stops[], dates, days,    │
                │  budget, travel_style, preferences, versions   │
                └───────────────────┬────────────────────────────┘
                                    │  generate(spec)  — the ONLY way a trip is built
                                    ▼
                ┌────────────────────────────────────────────────┐
                │  GENERATION MANIFEST  (immutable, per run)     │
                │  generation_id, spec_version, model ids,       │
                │  provider versions, started/completed_at       │
                └───────────────────┬────────────────────────────┘
                                    ▼
                ┌────────────────────────────────────────────────┐
                │  TRIP GENERATOR  (one pipeline, 0..N stops)    │
                │  plan legs → search providers → build graph →  │
                │  itineraries → costs → ATOMIC COMMIT           │
                └───────┬───────────────┬───────────────┬────────┘
                        ▼               ▼               ▼
              ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐
              │ TRIP GRAPH   │  │ ITINERARY    │  │ SEARCH STORE      │
              │ nodes, legs, │  │ days per     │  │ batches + results │
              │ sequencing,  │  │ stop (owned  │  │ (permanent IDs,   │
              │ selections,  │  │ by nodes)    │  │ raw payloads,     │
              │ REFERENCES   │  │              │  │ freshness TTL)    │
              └──────┬───────┘  └──────────────┘  └─────────┬─────────┘
                     │      graph stores result_id REFS      │
                     └───────────────► ◄────────────────────┘
                                    │
                                    ▼
                          ┌──────────────────┐
                          │ BOOKING OBJECTS  │
                          │ provider raw,    │
                          │ hydrated by ID   │
                          │ at read/booking  │
                          └──────────────────┘
```

**The rule:** the **spec** is the source of truth for *what the trip is*. The
**graph** is derived from the spec (plus live provider data at generation
time) and is the source of truth only for *what was found and what the user
selected*. The **store** is the source of truth for *provider payloads*. The
graph never owns payloads; regeneration never reconstructs the spec from the
graph.

### What each layer owns

| Layer | Owns | Never owns |
|---|---|---|
| Spec | origin/dest/stops/dates/days/budget/style/preferences, `spec_version` | search results, selections, derived dates |
| Graph | nodes, legs, hop structure, sequencing, resolved dates, `selection` per leg, `result_id` references, denormalized display fields, `graph_version`, `built_from_spec_version` | provider raw payloads, recommendations (always derived) |
| Search store | immutable provider results with permanent IDs, batch metadata (`scope_id`, `params_hash`, `searched_at`, `expires_at`) | trip structure, user choices |
| Views/memory | per-result user overrides (hide/favorite/pin), durable rules | — |
| Projection (code, not storage) | legacy-shaped `transport`/`itinerary`/`costs` blocks, recommendations | anything persisted |

---

## 2. Data flow

**Generation (write):**

```
POST /api/v2/plan  {spec}
  → validate + persist spec (trip row, status=planning, spec_version=1)
  → enqueue on generation queue (existing: max_concurrent_generations/worker)
  → GENERATOR:
      1. plan_route per leg          (deterministic LLM: temp-0 + seed + cache)
      2. search providers per leg    (bounded parallelism, breakers, retries)
      3. persist search batches      (store FIRST → get permanent result_ids)
      4. build graph                 (references + denormalized display fields)
      5. itinerary per stayed stop   (parallel, seeded for determinism)
      6. costs, weather, documents
      7. ATOMIC COMMIT               (one CAS-guarded snapshot write)
  → SSE stream carries progress + preview patches (decoration only)
  → frontend refetches GET on `complete` (authoritative)
```

**Read:**

```
GET /api/v2/plan/{id}
  → load trip row (spec + graph + meta)              [1 query]
  → collect result_ids referenced by graph
  → hydrate raws by ID from search_results           [1 query]
  → project: graph → transport/itinerary/costs blocks (+ freshness flags)
  → return complete response
```

No frontend reconstruction. No SSE dependency. No browser memory dependency.

---

## 3. Storage model

Tables (all existing; changes marked **NEW**):

**`trip_states`** — one row per trip.
- `id`, `user_id`, `visibility`, `status`, `last_seq` (existing; `last_seq` is
  the optimistic-lock/CAS token — keep).
- `state` jsonb, restructured into disciplined sections:
  - `spec` **NEW** — the immutable specification. Structured-action edits
    append a new revision (`spec.version` increments); the previous revision is
    kept in `spec_history` (bounded, e.g. last 10). Never mutated in place.
  - `graph` — v2 shape (**NEW**): options carry
    `{result_id, mode, operator, departure, arrival, duration_min,
    fare_per_person, currency, available}` — **no `raw`, ever, in memory or at
    rest**. Top-level `graph.version: 2` and `graph.built_from_spec_version`.
  - `itinerary`, `costs`, `stay`, `destination`, `documents`, `meta` — as
    today (derived sections, regenerated by projection where possible).
  - `meta.generation` **NEW** — the **Generation Manifest**, an immutable
    record of exactly what the last successful generation used:

    ```json
    { "generation_id": "…", "spec_version": 4,
      "route_model": "gemini-2.5-flash", "itinerary_model": "gemini-2.5-flash",
      "provider_versions": {"flight": "v4", "hotel": "v2", "train": "v1", "bus": "v1"},
      "engine_version": "…", "started_at": "…", "completed_at": "…", "status": "complete" }
    ```

    Model ids come from config at run time; provider versions from a
    per-adapter constant bumped whenever a scraper/parser changes shape. Six
    months of prompt/ranking evolution later, any old trip still states
    exactly which models and provider parsers produced it — reproducibility
    and debuggability survive engine changes.
  - `meta.checksums` **NEW** — integrity seals, recomputed on **every**
    committed snapshot (not only generations):

    ```json
    { "spec": "sha256:…", "graph": "sha256:…", "search": "sha256:…",
      "algo": "sha256-canonical-json-v1" }
    ```

    `spec` and `graph` hash the canonical JSON of those sections;
    `search` hashes the sorted list of `result_id`s the graph references
    (empty list for legacy graphs). Stamped inside the same CAS-guarded write
    as the data itself, so seal and payload cannot diverge. GET recomputes and
    compares: mismatch ⇒ the response is marked `integrity: "corrupted"`
    (logged + alerted) instead of silently serving bad state — the read still
    returns the data (degraded-visible beats blank), but the corruption is
    explicit and actionable (restore from snapshot/events).

**`trip_state_events`** — append-only event log (existing, kept). The critical
commit is one event; `seq` ordering + snapshot replay already handle lost
responses (see `store.append_events`).

**`search_batches` / `search_results`** — existing. `search_results.id` (uuid)
**is** the permanent option identifier (`flight_option_id` etc. are all just
`result_id` with `item_type` discriminating). Additions:
- Batch `searched_at` (exists as `created_at`) + `expires_at` (exists) drive
  freshness flags — results are **never deleted for being stale**, only
  superseded (a replacing batch swaps in atomically per scope; the delete
  cascade in `sync_store` is replaced by insert-then-repoint, §6).
- **NEW invariant:** batches are written *before* the graph commit that
  references them, so a committed graph never references missing rows.

**`trip_snapshots`** **NEW** — immutable pre-change archives:
`(id, trip_id, generation_id, reason, spec_version, last_seq, state jsonb,
created_at)`. Written **before** every regeneration/structural rebuild (and
available to any risky write). Restore = copy an archived state back through
the normal CAS write path. Bounded retention (last N per trip + TTL).

**`trip_result_views`, `trip_memory`, `trip_recommendations`** — existing,
unchanged roles.

**Schema/versioning fields:** `spec.version` (spec schema), `spec_history[]`
(revisions), `graph.version` (graph schema: 1 = legacy fat/thin, 2 =
references-only), `graph.built_from_spec_version` (drift detection). Old trips
keep `graph.version: 1` and are read through the legacy path forever — no
in-place migration required (see §11).

---

## 4. Read flow (refresh reliability)

```
load row → spec + graph + meta
  graph.version == 2:
      ids = collect result_ids from graph (legs + stays)
      raws = SELECT id, raw FROM search_results WHERE id IN (ids)   ← exact, no fuzzy
      project(graph, raws) → transport buckets, stays, itinerary, costs
  graph.version == 1 (legacy):
      existing path: derive/ensure graph, hydrate_graph_option_raws (fuzzy
      scope/city + operator/clock/fare matching — already built and verified)
  missing raws (expired hard-delete, partial store failure):
      option renders from its denormalized fields (operator/times/fare) —
      degraded card, never a blank screen
response carries per-leg freshness:
  { searched_at, stale: now > expires_at }  → UI shows "prices from 3 days ago"
```

Properties: deterministic (same storage → same response), 2 queries, zero
LLM/scraper calls, tolerant of store gaps (denormalized fallback), tolerant of
staleness (flag, don't blank).

## 5. Write flow (generation)

1. **Spec persist first.** The trip row with its spec is committed before any
   generation work. A crash at any later point leaves a resumable trip:
   `status=planning` + spec present ⇒ the UI offers "resume", which re-enqueues
   `generate(spec)` — idempotent because generation is a pure function of
   (spec, live provider data) and commits atomically.
2. **Store before graph.** Each leg's search results are persisted to
   `search_batches`/`search_results` (resilient write path, existing
   `run_db_resilient`) and the returned ids are stamped into the graph options.
   If the store write ultimately fails for a leg, generation continues but that
   leg's options carry `result_id: null` + denormalized fields, and the batch
   is queued for the flush worker — degraded, recoverable, never blocking.
3. **One atomic commit.** The generator buffers all sections in memory (the
   `_Emitter` critical-commit pattern — already built) and lands ONE
   CAS-guarded `append_events` write containing spec echo, graph, itinerary,
   costs, meta. Intermediate SSE emissions are *not* individually persisted.
   Consequences:
   - a partially generated trip never overwrites a valid one;
   - regeneration failure leaves the previous committed state untouched;
   - duplicate/concurrent commits are serialized by the per-trip lock +
     `last_seq` CAS (existing `store.py` machinery — kept as-is).
4. **Post-commit, best-effort:** reco index scoring, My Trips mirror, user
   memory distill — all off the critical path (existing pattern).

## 6. Regeneration flow

Regeneration is **the same function as generation**: `generate(spec)`.

```
structured action mutates spec → spec.version++ (old revision → spec_history)
  → ARCHIVE: current committed state → trip_snapshots (reason="pre-regenerate")
  → new Generation Manifest created (generation_id, spec_version, models…)
  → generate(spec) runs OFF the live state (in memory)
  → on success: atomic commit swaps in new graph + manifest + fresh checksums
  → on failure/crash: live state untouched (nothing partial was committed);
      if a commit itself is later found corrupted, RESTORE the archived
      snapshot through the normal CAS write path — instant rollback, no
      downtime, no data loss
```

- **Selection carry-forward:** before commit, the generator re-applies old
  `leg.selection`s where an equivalent option exists in the new results
  (match by mode + operator + service number/ext_id); unmatched selections are
  dropped and reported in the response ("your 6 AM train is no longer
  available — we picked the closest alternative").
- **Concurrent regeneration guard:** a per-trip regeneration mutex (DB-level:
  refuse if `meta.generation.status == running` and started < N minutes ago;
  in-process lock is not enough across workers). Duplicate structured-action
  requests carry a client `action_id` and are deduped.
- **Search reuse:** unchanged legs (same origin/dest/date `params_hash`) reuse
  live batches via the existing `find_live` 24 h cache — a date change only
  re-searches affected legs.

## 7. Structured action flow

Actions are first-class API citizens (extending the existing `EditAction`
LLM-bypass path, `endpoint.py:39`):

| Action | Class | Effect |
|---|---|---|
| `select_flight/train/bus/hotel` (`select_leg_option`, `select_stop_hotel`, …) | **Selection** | updates `leg.selection` / `stay.selected_id` only; one small commit; no search, no LLM. Already shipped. |
| `add_stop`, `remove_stop`, `change_dates` | **Structural** | spec mutation → `generate(spec)` (§6). |
| `change_budget`, `change_style` | **Re-rank** | spec mutation → re-rank existing stored results (scoring is deterministic code) + re-run only the per-stop itinerary LLM calls; **no provider re-search**. |
| `regenerate` | **Structural** | `generate(spec)` at current spec_version. |
| `refresh_prices` (optional, later) | **Freshness** | re-search stale legs only; graph structure unchanged. |

Copilot chat remains behind `COPILOT_ENABLED=false`; its agent tools ultimately
call the same builder cores, so re-enabling it later costs nothing
architecturally.

## 8. Failure recovery strategy

| Failure | Behavior |
|---|---|
| Browser refresh / SSE drop | GET rebuilds the full response from storage (§4). SSE is progress decoration; the frontend refetches on `complete` and renders only from GET responses + selection acks. |
| Duplicate POST /plan | rate-limited (10/h, existing) + client idempotency: same spec hash within N minutes returns the existing planning trip instead of a new row. |
| Server/process restart mid-generation | trip stays `status=planning` with spec committed; a reaper (or next open) offers resume → re-run `generate(spec)`; nothing partial was committed. |
| Partial provider failure | leg marked `no_route`/degraded with gateway alternatives; generation completes; freshness/absence surfaced per leg — never a failed trip because one provider 500'd. Existing per-provider breakers (Booking/EMT/RedBus/… `max_concurrency` 5–10, `failure_threshold` 2–3) keep a sick provider from stalling the pass. |
| Vertex saturation | region cooldown + rotation (shipped), route-plan cache (shipped), `breaker_gemini` (existing) sheds load on sustained outage; itinerary retry once then degraded ("itinerary pending — regenerate"). |
| Supabase TLS storms | existing: per-trip lock + CAS + fresh-client retries + FIFO flush DLQ (`flush.py`) — unchanged, now with strictly fewer, larger writes (one commit vs. patch streams). |
| Store write lost after retries | graph commits with `result_id: null` + denormalized fields; read path degrades gracefully; flush worker converges the store. |
| User reopens days later | reads serve the last committed snapshot with `stale: true` freshness flags; explicit `refresh_prices`/`regenerate` action re-searches — nothing auto-mutates on read. |

## 9. Data consistency guarantees

1. **Single writer per trip:** in-process lock + `last_seq` CAS + event replay
   (existing, `store.append_events`) — concurrent writers cannot interleave
   partial states; a lost UPDATE response is recognized and not duplicated.
2. **Atomic generation:** one commit per generation; readers see old-complete
   or new-complete, never mixed.
3. **Referential integrity by write order:** store rows exist before any graph
   that references them (§5.2). Missing refs are possible only through the
   declared degraded path, and the read path tolerates them.
4. **Spec monotonicity:** `spec.version` strictly increments; every graph
   records `built_from_spec_version`; a mismatch (spec edited, regeneration
   failed) is visible to the UI as "changes pending — regenerate".
5. **Idempotent side-writes:** store mirror is content-hashed (existing
   `sync_store._signature`); reco scoring and My Trips upserts are keyed.
6. **Events append-only, snapshot = fold(events):** unchanged. `graph_version`
   / `spec.version` gate all future schema migrations (migrate-on-read, never
   in-place bulk rewrites).
7. **Self-sealing snapshots:** every committed state carries checksums of
   itself, stamped in the same write; GET verifies them, so storage/transport
   corruption (the `bad_record_mac` class) is detected at read time instead of
   silently rendered.
8. **Provenance:** every trip names the manifest that built it (models,
   provider parser versions, timestamps) — reproducibility survives prompt,
   ranking, and scraper evolution.
9. **Rollback:** pre-regeneration snapshots make any structural rebuild
   reversible through the normal write path.

## 10. Reliability improvements over today

- Frontend renders from GET only; SSE becomes optional decoration (today a
  refresh loses whatever only SSE delivered — root cause of the transport bug).
- Graph never carries raw payloads (today: fat in memory, thinned at persist —
  two shapes, sync-order-sensitive, and the source of the fat/skinny bug
  class). One shape everywhere ends the fat/thin duality.
- Exact ID hydration replaces fuzzy matching for all new trips.
- One generation pipeline (today: supervisor for simple trips, builder handlers
  for edits — the return-edge `routes: []` bug lives in that duality).
- Atomic commit replaces incremental patch persistence during generation
  (fewer, larger writes → fewer TLS-storm exposure windows).
- Regeneration guarded against concurrency and duplication.
- Freshness is explicit instead of silent staleness.

**Concurrency model (mostly existing, tuned):** generation queue caps
concurrent generations per worker (existing); per-provider breakers with
per-provider concurrency caps (existing); **NEW:** per-generation leg fan-out
cap (semaphore ~3 legs in flight) so a 5-stop trip cannot fire 12 legs × 4
providers at once; Vertex calls already gated by `breaker_gemini` + regional
rotation. Retries: transport-level with backoff (existing scraper read-retry
work), never unbounded.

## 11. Migration plan from the current architecture

- **M0 (done, uncommitted):** refresh hydration fix, Vertex cooldown,
  route-plan cache. Commit as-is — M0 is also the legacy read path forever.
- **M1 — Copilot flag + read-authority:** `COPILOT_ENABLED=false` gating only
  free-text edits; frontend hides console, refetches GET on SSE `complete`,
  and renders exclusively from GET. *(small; immediately kills the class of
  "SSE showed it, refresh lost it" bugs)*
- **M2 — Spec formalization + integrity layer:** `state.spec` + versions
  written on create; backfill script derives spec from `state.request` for all
  existing trips (pure data-shape backfill, no graph touch). Structured-action
  endpoints validate against spec. **Integrity layer ships here, on the
  CURRENT engine** (it does not wait for M3): Generation Manifest written by
  the existing generator's critical commit; checksums stamped on every
  committed snapshot + verified on GET; `trip_snapshots` table + archive
  before structural rebuilds + restore helper.
- **M3 — Unified generator, graph v2:** extract `build_leg` core from
  `builder.handle_add_leg`; new `generate(spec)` pipeline (0..N stops, store-
  first ids, atomic commit, seeded itineraries). `POST /api/v2/plan` switches
  to it. `/api/v1/plan` (mobile app) adapts request→spec and calls the SAME
  pipeline, projecting the legacy response shape — one engine, two façades.
  Supervisor’s orchestration retires after a bake period.
- **M4 — Read path v2:** version-switched reader (v2 = exact ID hydration +
  freshness flags; v1 = today's fuzzy fallback). Frontend adds freshness UI.
- **M5 — Structured actions:** spec-mutating actions + regeneration flow +
  selection carry-forward + regeneration mutex + `action_id` dedupe. Frontend
  action bar (add/remove stop, dates, budget, style, regenerate).
- **M6 — Cleanup:** demote recommendation LLM to muscle tier; delete
  generator dual-write scoping (`going`/`return`) once v2 trips dominate;
  legacy path stays but frozen.

Old trips are never migrated in place: they keep `graph.version: 1` and the
legacy read path. New generations of an old trip (any structural action)
naturally produce a v2 graph — migration by attrition.

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Store write becomes critical-path (was fire-and-forget) → generation latency/failure coupling | resilient writes with bounded retry; on exhaustion, degrade to `result_id: null` + denormalized fields + flush-queue convergence — generation never blocks on the store |
| Read latency +1 query (hydration) | single `IN (ids)` indexed query; measure; per-trip response cache keyed on `last_seq` if needed |
| Regeneration wipes user intent | selection carry-forward + explicit report of dropped selections; spec_history enables undo later |
| One pipeline regression breaks BOTH v1 and v2 façades | bake M3 behind a flag per endpoint; fixture replay tests: regenerate the 27 prod trips' specs against recorded provider data |
| Free-trial quotas: structural actions are full regenerations | `find_live` batch reuse (only changed legs re-search), route-plan cache, re-rank class avoids regeneration entirely; regenerate rate limit per trip |
| Supabase remains a single point of failure | out of scope now; the atomic-commit + DLQ design already minimizes write windows; revisit read replicas when traffic justifies |
| Legacy fuzzy hydration mismatches (wrong raw on an option) | ≥2-signal matching (shipped); v2 trips eliminate the class; legacy path frozen, not extended |
| Longer wall-clock for many-stop generations | per-generation fan-out cap keeps providers healthy; SSE progress phases keep UX responsive; generation timeout budget raised proportional to leg count |

## 13. Recommended implementation order

1. **M0** — commit shipped fixes (today).
2. **M1** — copilot flag + GET-authoritative frontend (small, kills the bug class).
3. **M2** — spec formalization + backfill (small, unblocks everything).
4. **M3** — unified `generate(spec)` + graph v2 + atomic commit (the core; largest).
5. **M4** — read path v2 with exact hydration + freshness.
6. **M5** — structured actions + regeneration guards.
7. **M6** — cleanup + reco tier demotion + legacy freeze.

Each step is independently shippable and reversible; M3/M4 ride behind flags
until fixture-replay tests and a prod bake pass.
