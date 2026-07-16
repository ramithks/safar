# TripState Removal — Migration Plan

*Written 2026-07-16. Supersedes `docs/trip_engine_architecture.md` (v4) and
`docs/persistence_architecture.md` as the forward direction.*

**STATUS 2026-07-16 — EXECUTED through step 6.** Backend: `app/trips_doc/` +
`/api/v3/trips` built, 44 trips backfilled (twice), the old machinery DELETED
(−9,718 lines; `app/trip_state/` is now 1,371 lines of generation-time
build machinery: graph model/ranking, leg-search stabilization, skeleton +
gateway fallback). Frontend: v3 proxies + `lib/trip-doc.ts` view adapter +
polling `useTripDoc` hook; SSE/patch/copilot client deleted (−2,504 lines).
Live-verified: full generation (70s, progressive doc writes), selections,
change_budget re-rank. **Tables DROPPED 2026-07-16** (trip_state_events,
copilot_messages, trip_result_views, trip_recommendations, trip_memory,
search_results, search_batches; trip_states.state/last_seq/schema_version
columns) after converting all 46 trips; `convert.py` + the lazy-conversion
read path deleted with them. REMAINING: deploy both repos.*

**Goal:** every trip becomes one plain JSON document in Supabase. Generate →
save JSON → return JSON. Selections are a single-column UPDATE. No events, no
patches, no Redis streams, no graph projections, no snapshots, no replay.

---

## 1. Current Architecture

### 1.1 The stack, bottom to top

A "trip" today is spread across **eight coordinated mechanisms**:

| Layer | Where | What it does |
|---|---|---|
| Snapshot | `trip_states.state` (jsonb) + `last_seq` | Current TripState, CAS-guarded by `last_seq` |
| Event log | `trip_state_events` (trip_id, seq, patches) | Append-only JSON-Patch log; replay source |
| Patch protocol | `app/trip_state/patch.py` | set/merge/remove/append ops + per-agent path ownership |
| Live stream | Redis list `tripstate:{id}:stream` (`generator.py`) | SSE tail for the frontend; 24h TTL; doubles as read-repair |
| Flush queue | `app/trip_state/flush.py` | In-process FIFO retrying failed writes up to 30 min (TLS storms) |
| Integrity | `app/trip_state/integrity.py` + `trip_snapshots` | Generation manifests, sha256 checksums, pre-rebuild archives |
| Search store | `search_batches` / `search_results` (`batches.py`, `sync_store.py`) | Immutable provider payloads; graph holds `result_id` references |
| Graph + projections | `app/trip_state/graph.py` (1,038 lines) | Canonical multi-city graph; `transport`/`itinerary` blocks are *derived projections* regenerated on every read |

### 1.2 The write path (generation)

`POST /api/v2/plan` → insert `trip_states` row → background `run_generation`
(`generator.py`) → `compose.generate_from_spec` builds an in-memory `TripGraph`
(leg search, gateway fallback, per-stop itineraries, hotels) → **store-first
mirror** stamps permanent `result_id`s into `search_results` → ONE "critical"
emit: buffered patches + final sections go through `store.append_events`
(per-trip lock → re-read snapshot → replay orphaned events → CAS update →
seq-conflict retry loop → integrity restamp), while a *different, fat* variant
of the same patches (`client_patches`) streams to Redis for live viewers. On
TLS failure the whole buffer is handed to the flush queue. Then: My Trips
mirror upsert, referral award, memory distill, reco scoring.

### 1.3 The read path

`GET /api/v2/plan/{id}` does, in order:
1. Read row + **overlay every Redis stream event** on the snapshot
   (`_overlay_stream_events` — read-repair for writes stuck in the flush queue)
2. Verify sha256 checksums (`integrity.verify`)
3. **Hydrate**: fetch `search_results` rows and fuzzy-match raws back onto thin
   graph options (`retrieval.hydrate_graph_option_raws` — mode/operator/clock/
   fare matching, ≥2 signals)
4. **Project**: regenerate `transport`/`itinerary`/`costs` + the derived
   recommendation from the graph (`graph.projected_state`)
5. Stamp freshness (`batch_freshness` — more search-store selects)

Five mechanisms fire to serve one page load.

### 1.4 The edit path (a hotel click today)

`POST /edit {action: select_hotel}` → row read + Redis overlay replay → handler
mutates state + recomputes costs → `append_events` (lock/CAS/retry) → Redis
publish → My Trips mirror upsert → background search-store sync (3+ selects,
content-hashing) → **fat re-projection** (`client_patches` + re-hydration) so
the returning patches don't blank the client's option buckets. A click costs
~6 DB round-trips + Redis + a full graph reprojection.

### 1.5 Why it exists (and why it can go)

Almost all of this machinery is compensation for **two self-inflicted
problems**:

1. **Multiple writers streaming partial state** (supervisor phases, weather,
   copilot agent tools) → needed patches, seq, ownership, CAS, replay.
   The copilot is removed and generation now lands **one atomic commit**
   (`compose.py`) — there is effectively a single writer already.
2. **The skinny-at-rest design** (raw payloads in `search_results`, graph holds
   references) → needed hydration, projections, fat/skinny dual patches,
   sync-store mirrors. If the document simply stores render-ready options,
   all of it disappears.

The TLS-storm resilience (flush queue, orphan replay, read-repair) exists
because a generation performed *dozens* of read-modify-write cycles that were
individually non-idempotent. A full-document UPDATE is naturally idempotent —
`run_db_resilient` alone covers it.

---

## 2. Dependency Graph

### 2.1 The `app/trip_state/` package (7,336 lines, 20 modules)

| Module | Lines | Role | Fate |
|---|---|---|---|
| `graph.py` | 1,038 | Domain model + ranking + **projections + legacy derivation + thin-dump** | Shrink ~60% — keep in-memory build model + ranking; delete projections/derivation/thinning |
| `builder.py` | 965 | Leg search/stabilization + selection handlers + `client_patches` | Keep search internals; selection handlers become ~30-line doc updates; delete patch plumbing |
| `copilot.py` | 932 | `apply_edit` — structured actions + agent path + persistence/broadcast | **Delete** (selection logic moves to new endpoint) |
| `agent.py` | 684 | Copilot agent loop (flagged off) | **Delete** |
| `generator.py` | 474 | `_Emitter`, Redis stream, supervisor branch | **Delete** — replaced by ~100-line doc-writing runner |
| `retrieval.py` | 428 | Hydration, fuzzy raw matching, freshness, RetrievalService | **Delete** |
| `structural.py` | 365 | Copilot waypoint/activities tools | **Delete** |
| `compose.py` | 351 | Unified spec→trip generator | **Keep** — output becomes a doc, not patches |
| `batches.py` | 300 | Search store rows/batches | **Delete** (see §5 caveat on reco) |
| `store.py` | 288 | CAS append, orphan replay, seq conflicts, chat log | **Delete** — replaced by get/put doc |
| `sync_store.py` | 251 | Graph→store mirror, content hashing | **Delete** |
| `integrity.py` | 240 | Manifests, checksums, snapshot archive | **Delete** (keep one pre-regen archive INSERT, ~15 lines) |
| `models.py` | 223 | TripState pydantic tree | **Delete** — replaced by TripDoc model |
| `hotel_search.py` | 219 | Copilot landmark hotel re-search | Port to explicit "Search hotels" endpoint, drop patch output |
| `memory.py` | 169 | Trip-scoped preference rules | **Delete** (user-level `user_travel_memory` stays) |
| `patch.py` | 136 | Patch protocol + ownership | **Delete** |
| `flush.py` | 114 | Background write-retry queue | **Delete** |
| `views.py` | 73 | hide/favourite/pin per result | **Delete** (favourite becomes a doc field if wanted) |
| `my_trips.py` | 73 | Mirror into `trips` table | **Keep** (reads the doc instead) |

### 2.2 Everything outside the package

**API surfaces**
- `app/api/v1/trip_state/endpoint.py` (530 lines) — create/get/stream/edit/
  edit-stream/chat/actions. Mounted at BOTH `/api/v1/trip-state/*` (verify: no
  known client) and `/api/v2/plan/*` (website workspace).
- `app/api/v2/search.py` (140 lines) — options/results/views/recommendations/
  memory endpoints reading the search store; uses `trip_state.store` for auth.
- `app/api/v1/router.py`, `app/api/v2/router.py` — mounts.

**Services / workers**
- `app/agents/supervisor.py` — `save_legacy_tables=False` flag exists solely
  for the trip-state path; classic `/api/v1/plan` (mobile) also calls it and is
  **unaffected**.
- `app/reco/index.py` `score_batch` — writes `trip_recommendations` keyed by
  search-store batches; called from generator/compose/sync_store.
- `app/services/user_memory.distill_from_trip` — consumes the final state dict;
  trivially retargets to the doc.
- Referral award hook (`generator._award_referrer`) — keep, one call.
- Redis: used by trip streams (**delete**), and independently by limiter, reel
  jobs, reco cache (**stay**).

**Infra invariants coupled to TripState**
- `app/db/client.py` `run_db_resilient` idempotency rules — written around the
  append loop; survives unchanged (doc writes are idempotent upserts).
- `_overlay_stream_events` read-repair; flush-queue `has_pending` ordering
  rule — both die with the event system.

**Scripts (obsolete after removal)**
- `scripts/backfill_search_store.py`, `scripts/thin_stored_graphs.py`,
  `scripts/builder_e2e_multicity.py` (rewrite), `scripts/copilot_e2e_*.py`.

**Tests (11 files bound to the old machinery)**
`test_trip_state_store`, `test_agent`, `test_builder`, `test_compose_actions`,
`test_copilot_edits`, `test_edit_stream`, `test_generation_single_commit`,
`test_integrity`, `test_retrieval`, `test_search_batches`, `test_sync_store`.

**Database tables**
- `trip_states` (snapshot + `last_seq`), `trip_state_events`,
  `copilot_messages`, `trip_snapshots`, `search_batches`, `search_results`,
  `trip_result_views`, `trip_recommendations`, `trip_memory`.
- `trips` — My Trips mirror (**stays**; `trips.id == trip_states.id`,
  `planner="workspace"` marker the frontend routes on).

**Frontend (travbuddy_website, `/plan` workspace)**
- SSE client applying patch events to a local state tree
- Fat-vs-skinny patch awareness (recent commit e527528: "client-side fat
  transport projections to preserve UI state during streaming")
- `POST /edit {action}` for selections; `POST /actions` for structural changes
- `GET /chat` on mount (copilot vestige); possibly `/api/v2/trips/{id}/options`
- My Trips cards deep-linking `/plan/{id}`

---

## 3. Removal Plan (step-by-step, minimal risk)

Pre-scale product, single backend, one frontend consumer → a **staged cutover**
beats a long dual-write period. Every step is deployable and reversible until
step 6.

**Step 0 — Freeze & verify (half a day)**
- Confirm nothing calls `/api/v1/trip-state/*` (access logs) → retire the mount.
- Confirm which of `/api/v2/trips/{id}/options|results|memory|recommendations`
  the website actually calls (grep the website repo). Expectation: none or one.
- Copilot already flagged off (`copilot_enabled=False`) — no user-facing chat.

**Step 1 — New document module (1 day)**
- `app/trips_doc/model.py`: `TripDoc` pydantic model (§4.1) — flat, render-ready.
- `app/trips_doc/store.py`: `get_doc(id)`, `put_doc(id, doc)`,
  `update_fields(id, {json paths})` — plain reads/updates via `run_db_resilient`.
- `app/trips_doc/serialize.py`: `doc_from_graph(trip_id, spec, graph, extras)` —
  serializes the composer's in-memory `TripGraph` into the doc: **options
  inlined, trimmed, ranked at write time** (top ~10/mode/leg, top ~20 hotels,
  no `raw`), `recommendedId` persisted (the "derived-never-persisted" rule is
  deliberately dropped — regeneration recomputes it, which is all the freshness
  the product needs).

**Step 2 — New generation runner (1–2 days)**
- `run_generation_v2(trip_id, spec, user_id)`: wraps `compose.generate_from_spec`
  internals but emits **progressive doc writes** instead of patches: after each
  phase (transport found / itinerary built / complete) it serializes what
  exists and UPDATEs the row (`status`, `progress`, sections). 3–5 idempotent
  writes per generation, no Redis, no events, no emitter.
- Port `carry_forward_selections` to doc terms (match by option identity).
- Keep: generation queue/slot, weather task, My Trips upsert, referral award,
  memory distill, manifest → collapses to a simple `generatedAt` + model-name
  field on the doc.

**Step 3 — New API (1 day)** — new router, mounted at `/api/v3/trips` (or
behind a flag on `/api/v2/plan`; separate mount is cleaner for the frontend
switch):
- `POST   /trips` — create row (status=planning), spawn generation
- `GET    /trips/{id}` — return the doc verbatim (one SELECT)
- `PATCH  /trips/{id}/select` — `{kind: transport|hotel, scopeId, optionId}` →
  set `selectedId`, recompute `budget` totals (pure ~30-line function), one
  UPDATE, return updated `budget` + `updatedAt`
- `POST   /trips/{id}/actions` — same body as today (`add_stop`, `remove_stop`,
  `change_dates`, `change_budget`, `regenerate`): mutate `spec` on the doc,
  archive one pre-regen copy, spawn regeneration. `change_budget` = re-rank the
  already-inlined options + UPDATE (no re-search, no LLM).
- `POST   /trips/{id}/search-hotels` — explicit re-search (ports
  `hotel_search.py`), replaces the doc's hotel options.
- **Streaming:** none. The client polls `GET /trips/{id}` every ~2s while
  `status=="planning"`. Generation takes 30–90s with 3–5 section landings —
  polling is indistinguishable from SSE for this UX and deletes the entire
  stream/replay/reconnect surface. (If push is ever wanted back: Supabase
  Realtime on the row, zero backend code.)

**Step 4 — Backfill (half a day)**
- One-time script: for each `trip_states` row, run the **existing** read path
  (overlay → hydrate → project, i.e. exactly what `GET` serves today) and write
  the result through `doc_from_graph`-style trimming into the new shape.
  This is the last thing the old read machinery ever does.

**Step 5 — Frontend switch (2–3 days, travbuddy_website)** — see §7.

**Step 6 — Delete (1 day, the fun one)**
- Delete: `copilot.py`, `agent.py`, `structural.py`, `patch.py`, `store.py`,
  `flush.py`, `integrity.py`, `retrieval.py`, `sync_store.py`, `batches.py`,
  `views.py`, `memory.py`, `models.py`, `generator.py`, `hotel_search.py`,
  old `endpoint.py`, `api/v2/search.py`; shrink `graph.py`; the 11 old test
  files; the 4 obsolete scripts. (~7,000 lines.)
- After a 1–2 week soak: drop `trip_state_events`, `copilot_messages`,
  `trip_memory`, `trip_result_views`, `trip_recommendations`,
  `search_batches`/`search_results` (see §5 reco caveat); drop
  `trip_states.last_seq` + `schema_version`.

Total: **~1.5–2 weeks** including frontend.

---

## 4. New Architecture

### 4.1 The document

One row per trip; `doc` is what the UI renders — no projection, no hydration.

```jsonc
{
  "id": "uuid",
  "title": "Mumbai → Goa",
  "status": "planning | ready | error",       // top-level column too, for listing
  "progress": 100,                              // 0-100 while planning
  "spec": {                                     // what the user asked for
    "origin": "Mumbai", "destination": "Goa",
    "departureDate": "2026-08-01", "days": 4, "travelers": 2,
    "budgetTier": "mid", "interests": [],
    "stops": [{ "city": "Pune", "nights": 1 }]
  },
  "itinerary": [                                // one entry per stayed city, in order
    {
      "stopId": "stop-1", "city": "Pune", "nights": 1,
      "arrivalDate": "…", "departureDate": "…",
      "days": [ { "day": 1, "blocks": [ … ] } ],
      "hotels": {
        "options": [ { "id": "h-…", "name": "…", "pricePerNight": 3200,
                        "rating": 8.4, "photo": "…", "bookingUrl": "…" } ],
        "selectedId": "h-…", "recommendedId": "h-…", "searchedAt": "…"
      }
    }
  ],
  "transport": [                                // one entry per leg, in order (incl. return)
    {
      "legId": "leg-1", "from": "Mumbai", "to": "Pune", "date": "2026-08-01",
      "options": [ { "id": "t-…", "mode": "train", "name": "12124 Deccan Queen",
                      "depart": "17:10", "arrive": "20:25", "farePerPerson": 550,
                      "bookingUrl": "…" } ],
      "selectedId": "t-…", "recommendedId": "t-…",
      "searchedAt": "…", "status": "ok | no_route"
    }
  ],
  "weather": { … },
  "documents": { … },
  "destination": { "name": "Goa", "description": "…", "coverPhotoRef": "…" },
  "budget": { "transportTotal": 4400, "stayTotal": 9600,
               "total": 21000, "perPerson": 10500 },
  "generatedAt": "…", "updatedAt": "…"
}
```

Design rules:
- **Options are inlined and trimmed.** Display + booking fields only, capped.
  No provider `raw` anywhere. Expected doc size 100–400 KB — one fast row read.
- **Recommendation is persisted.** Computed once at generation/re-rank time.
- **The graph is gone from storage.** Order is the array order;
  `transport[i]` connects `itinerary` cities implicitly. The in-memory
  `TripGraph` survives only *inside* generation as working structure.
- **`updatedAt` is the only concurrency control.** Single owner per trip;
  selections write disjoint fields via `jsonb_set`, so even two tabs can't
  corrupt anything worse than last-write-wins on one selection.

### 4.2 Request flows

```
Generate:   POST /trips → row(status=planning) → bg: search → doc UPDATE ×4 → status=ready
Read:       GET /trips/{id} → SELECT doc → return          (client polls this while planning)
Select:     PATCH /trips/{id}/select → jsonb_set selectedId + budget → return budget
Structural: POST /trips/{id}/actions → mutate spec → archive → regenerate (bg)
```

AI/search is invoked **only** by: generate, regenerate, actions that change
the route/dates, explicit hotel/transport re-search. Selections never touch it.

---

## 5. Database Changes

**Keep `trip_states` as the physical table** (optionally rename later) — ids
are baked into My Trips deep-links (`trips.id == trip_states.id`) and share
URLs; reusing it means zero id migration.

```sql
-- 1. loosen: state column becomes the TripDoc; keep useful listing columns
ALTER TABLE trip_states ADD COLUMN IF NOT EXISTS progress int NOT NULL DEFAULT 0;
-- (id, user_id, title, status, visibility, created_at, updated_at stay)

-- 2. backfill state → new doc shape (script, step 4)

-- 3. after soak, drop the machinery
ALTER TABLE trip_states DROP COLUMN last_seq, DROP COLUMN schema_version;
DROP TABLE trip_state_events;
DROP TABLE copilot_messages;
DROP TABLE trip_memory;
DROP TABLE trip_result_views;
DROP TABLE trip_recommendations;
DROP TABLE search_batches CASCADE;   -- see caveat below
-- trip_snapshots: KEEP (one INSERT before each regeneration — 15 lines of
-- insurance, bounded retention; cheap enough to justify)
```

**Reco caveat:** `search_results` also feeds reco *training* signals. The
dedicated `transport_options` table (reco engine) already captures transport
for training independently, and hotel identity lives in the `hotels` master
table. Verify `app/reco` reads nothing else from `search_results` before the
DROP; if it does, keep the table as a fire-and-forget training sink (writes
stay best-effort, no trip read path touches it).

RLS: existing `trip_states` policies (owner CRUD + link/public SELECT) carry
over unchanged.

---

## 6. Backend Changes

**Disappears** (~7,000 lines): patch protocol, event log + CAS/orphan-replay/
seq machinery, Redis trip streams + SSE endpoints, flush queue, read-repair
overlay, checksums/manifests, graph projections + legacy derivation + thin
dump + fat/skinny dual patches, hydration/fuzzy matching, search-store
mirroring + content hashing, view layer, trip memory, copilot/agent/structural
edit engine, chat transcript, `save_legacy_tables` special-casing.

**Remains** (unchanged): everything that actually finds trips — scrapers,
`plan_trip` orchestrator, journey routing, hotel services, scoring, weather,
places, `compose`'s leg-stabilization/gateway logic, generation queue,
`curl_pool`/`run_db_resilient` (the SSL-storm fixes at the source), classic
`/api/v1/plan` for mobile, My Trips, groups, reels, reco engine's public
surfaces (`/recommend`, `/hotels`).

**Becomes simpler:**
- `graph.py` → in-memory build model + ranking only (~400 lines)
- `builder.py` selection handlers → one ~30-line pure function
  `apply_selection(doc, kind, scope_id, option_id) -> doc`
- `generator.py` → ~100-line runner: phases write the doc, done
- `endpoint.py` (530 lines) → ~150-line CRUD router
- Error handling: a failed selection write is just a 500 the client retries —
  no queue, no ordering rules, no "was my edit lost?" ambiguity

---

## 7. Frontend Changes (travbuddy_website `/plan`)

- **One state object.** The workspace holds the TripDoc from `GET /trips/{id}`
  (React Query/SWR). Delete: the SSE client, patch applier, fat/skinny
  projection handling (commit e527528's whole problem class), chat mount call,
  stream reconnect logic.
- **Generation UX.** After `POST /trips`, poll `GET` every ~2s while
  `status=="planning"`; render sections as they appear (`progress` drives the
  bar). Same progressive feel, no stream.
- **Selection = optimistic local write.** Set `selectedId` in the local doc
  (0 ms perceived), fire `PATCH /select`, reconcile `budget` from the
  response; on failure, revert + toast. No re-render cliff, no bucket-wipe
  hazard — options never leave the client.
- **Structural actions.** Same buttons → `POST /actions` → doc flips to
  `planning` → resume polling. Regeneration response includes carried-forward
  selections automatically (they're in the new doc).
- **Rendering.** The doc is the render model: `transport[]` and `itinerary[]`
  map 1:1 to the journey board and day cards. Delete any graph-awareness.
- **My Trips** unchanged (`planner="workspace"` cards still deep-link
  `/plan/{id}`).

---

## 8. Performance Improvements

| Operation | Today | After | Why |
|---|---|---|---|
| Select hotel/ride | ~0.8–2s server (row read + Redis overlay + CAS write + event insert + Redis publish + mirror upsert + store sync + fat re-projection) | **~30–80 ms** (one `jsonb_set` UPDATE); **0 ms perceived** (optimistic) | 6+ round-trips → 1 |
| Trip page load | ~300–900 ms (read + overlay + verify + hydrate selects + fuzzy match + project + freshness selects) | **~30–60 ms** (one SELECT) | 5 mechanisms → 0 |
| Generation writes | dozens of read-modify-write cycles + Redis + store mirror + reco scoring | 4–6 idempotent UPDATEs | storm surface shrinks ~10× |
| Regeneration | archive + spec bump + event append + stream reset + rebuild | archive INSERT + rebuild | |

Bottlenecks that vanish entirely: per-trip write serialization (locks/CAS/seq
conflicts), Redis round-trips on every read *and* write, hydration fuzzy-match
CPU, projection recompute per read, flush-queue ordering stalls, the
read-repair replay on every request. The remaining latency is where it belongs:
scrapers and Gemini during generation only.

---

## 9. Risks

1. **Mid-generation refresh** — the partial doc must render sanely. Mitigate:
   progressive section writes (step 2) + frontend renders whatever exists.
2. **Lost selections on regeneration** — `carry_forward_selections` must be
   ported faithfully (match options by identity, not index). Test explicitly.
3. **Backfill fidelity** — old trips must convert through the *existing* read
   path (hydrate + project) or thin graphs lose their option buckets. Run the
   converter while the old code still exists; spot-check the prod trips that
   exercised hydration (`b997937c`, `756ebf34`).
4. **Concurrent tabs** — last-write-wins per field. Acceptable for a
   single-owner product; field-scoped `jsonb_set` keeps blast radius to one
   selection. Do NOT whole-doc-PUT from the client.
5. **TLS storms during the 4–6 generation writes** — covered by
   `run_db_resilient` (idempotent UPDATEs); worst case a section lands late and
   the next poll picks it up. The flush queue's 30-min guarantee is gone —
   accepted, since a lost mid-write no longer corrupts anything (next write
   overwrites wholesale).
6. **Silent functionality loss** — features quietly riding the old surface:
   trip-scoped memory rules, hide/favourite views, `/results/{id}` raw
   hydration, freshness badges, `link` visibility live-follow (shared viewers
   now poll). Verify each against real frontend usage in step 0; most are
   believed unused.
7. **Reco training regression** — if `search_results` feeds training, dropping
   it silently starves the reco engine (§5 caveat).
8. **Audit/debug trail** — the event log was a forensic record of every
   mutation. Replacement: log selections/actions to observability (structured
   logs already exist); `trip_snapshots` covers pre-regen restore.
9. **Doc size creep** — inlined options must stay capped; add a size assertion
   (warn > 500 KB) in `put_doc`.

---

## 10. Cleanup Opportunities (unlocked afterwards)

1. **Redis leaves the trip path entirely** — remaining uses (limiter, reel
   jobs, reco cache) are isolated; prod Redis becomes non-critical for
   planning.
2. **Retire the supervisor branch in generation** — `unified_generator` is
   default-true; the composer is the only trip-state path. Delete the flag and
   the `trip_state_from_merged` lift. (Classic `/api/v1/plan` keeps the
   supervisor for mobile — separate surface.)
3. **Retire `/api/v1/trip-state` mount** (after log verification) — one
   workspace surface.
4. **Simplify `db/client.py`** — the append-loop idempotency commentary and
   POST-skipping retry rules can shrink once the only trip writes are upserts.
5. **Docs pruning** — mark `trip_engine_architecture.md`,
   `persistence_architecture.md`, `trip_graph.md`, `simplification_plan.md` as
   historical; one new short doc describes the CRUD model.
6. **Test suite** — 11 machinery test files → ~4 small ones (doc serializer,
   selection function, actions/spec mutation, generation smoke).
7. **`copilot_enabled` flag, `copilot_messages` table, chat endpoints** — the
   last copilot vestiges go.
8. **Frontend deletion** — SSE client, patch types, projection workarounds; the
   workspace becomes a plain fetch-render-mutate app.
9. **Future features get cheaper** — share/clone = copy one row; export/print =
   serialize one doc; trip versioning (if ever wanted) = copy doc to
   `trip_snapshots`; group trip collab = Supabase Realtime on the row.

---

## Appendix — decisions made against preservation (per the brief)

| Kept-today mechanism | Why it's safe to delete |
|---|---|
| Event sourcing / replay | Single writer per trip now; no consumer of history except read-repair, which dies with it |
| Patches + ownership | Only needed when multiple agents streamed partial writes; copilot is gone |
| Redis stream + SSE | Polling a 100 KB doc every 2s during a 60s generation is strictly simpler and imperceptibly different |
| Skinny graph + search store on the read path | Trimmed inline options make the doc self-sufficient; `raw` was only needed for booking parsing, done at write time |
| Derived-never-persisted recommendation | Persisting `recommendedId` costs nothing and is recomputed on every regeneration — the only time it can change |
| Checksums/manifests | Defended against a write-amplification storm this design no longer produces |
| Flush queue + read repair | Idempotent whole-section writes + `run_db_resilient` replace both |
| Fat/skinny dual patch projection | No patches, no projection — the doc is the UI model |
