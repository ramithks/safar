# TravBuddy Recommendation Engine — Engineering Design Document

**Status:** Design (v1) · **Owner:** AI Engineering · **Last updated:** 2026-07-13
**Audience:** backend + ML engineers building the ranking/recommendation stack.

> This is an internal design doc, not a whitepaper. It is grounded in the code that
> already exists in this repo. Where a component already exists, this doc says
> *"extend `path/to/file.py`"* rather than pretending we start from zero. Read
> [`docs/trip_graph.md`](./trip_graph.md) first — the recommender operates over the
> trip graph (stops + legs), not a flat itinerary.

---

## 0. What we already have (do not rebuild)

| Concern | Where it lives today | Role in the new design |
|---|---|---|
| Live scraper results, normalized + enriched | `app/services/{booking,agoda,easemytrip,ixigo}/hotels.py`, `{booking,easemytrip,gotogate}/flights.py`, `railway.py`, `bus/{redbus,flixbus}.py` | **Candidate source (L1)**. Already emit subscores, savings, badges, `social_proof`, `baggage_kg`, `fare_type`, `layover_airports`. |
| Deterministic weighted ranking | `app/services/scoring.py` → `score_hotels()`, `score_flights()`, `rank_score` + `quality_score` (0–100), tunable weight dicts, `why_tags` | **Fast feature ranker (L2)**. Keep; extend with new features + absent-field-aware weights. |
| LLM pick + explanation | `app/agents/recommendation_agent.py` → `recommend_hotels/flights` over ≤8 candidates, budget-aware, memory-aware | **Re-ranker + explainer (L3)**. Keep; generalize to journeys + structured output. |
| Journey trade-off reasoning | `app/services/journey_reasoning.py` (deterministic hotel-night insight) | **Journey scoring seed (Part 5)**. Extend into `journey_score`. |
| Traveller profile / learning | `app/services/user_memory.py` + `user_travel_memory` (JSONB), `record_signals()`, `distill_from_trip()`, `memory_grounding()` | **Traveller Understanding Engine store (Part 2)**. Extend with structured profile + embedding. |
| Hotel identity cache | `hotels` master table, upserted by `app/api/v1/plan_trip/plan.py::_hotel_master_rows` (`on_conflict=source,source_hotel_id`) | **SEO profile + hotel embedding foundation (Part 12)**. |
| LLM infra | `app/services/vertex.py` → `brain()` (`gemini-2.5-flash`), `muscle()` (`gemini-2.5-flash-lite`), `generate_json()` | Intent extraction, tagging, re-rank, explanations. |
| Redis | `settings.redis_url` (Celery broker today) | Cache + feature store + online signals. |
| **Missing entirely** | — | **Embeddings / pgvector, feature store, feedback event log, transport persistence, cold-start priors.** Greenfield — Parts 6, 9, 12. |

**Design principle:** the recommender is a thin, fast layer over data the scrapers
already produce. The scrapers are the latency floor (multi-second, network-bound).
The recommender's own budget is **< 120 ms deterministic + one optional streamed
LLM re-rank over ≤ 8 items**. It must never add a blocking second to the hot path.

---

## Part 1 — System Architecture

### 1.1 The pipeline

```
User Query (chat / workspace intake)
   │
   ▼  ①  Intent Extraction ......... muscle LLM, cached ....... ≤ 400 ms (miss) / ~0 (hit)
   ▼  ②  Traveller Profile ......... Postgres + Redis merge ... ≤ 15 ms
   │       (explicit intake ⊕ learned memory ⊕ live session signals)
   ▼  ③  Candidate Retrieval ....... scrapers ∥ master-table
   │       scrapers dominate (2–8 s); DB recall ≤ 10 ms
   ▼  ④  Hard Filtering ............ in-memory predicate ...... ≤ 5 ms      →  all valid
   ▼  ⑤  Feature Scoring (L2) ...... scoring.py extended ...... ≤ 30 ms     →  TOP 50
   ▼  ⑥  Semantic Re-rank .......... pgvector cosine, top-50 .. ≤ 15 ms     →  TOP 15
   ▼  ⑦  LLM Re-rank (L3) .......... brain LLM over top-8 ..... ~1–2 s, STREAMED, optional  →  TOP 1
   ▼  ⑧  Explanation Generation .... folded into ⑦ or deterministic
   ▼  ⑨  Persist + Learn ........... async, off the hot path ... 0 ms user-visible
   ▼
Response (ranked list + one hero pick + "why" + alternatives)
```

**Funnel order (decided):** filter → **feature score → top-50** → **semantic re-rank →
top-15** → LLM → top-1. Cheap deterministic scoring runs on *everything* and cuts the
field to 50 before the vector step touches it, and the vector step cuts to 15 before
the LLM sees ≤ 8. We never embed-compare hundreds of candidates or send more than a
handful to Gemini — each stage is strictly cheaper than the last and only sees the
survivors of the one before it.

### 1.2 Why each stage exists

- **① Intent Extraction** — converts free text ("cheap Goa trip for me and my gf, chill, near the beach") into a typed `TripIntent`. Without it, ranking has no objective. It is the *only* place raw language becomes structured; everything downstream is numeric/structured. Cached hard on `(normalized_text, user_id)` because users re-ask the same thing.
- **② Traveller Profile** — merges three time-horizons of preference: **explicit** (this intake), **learned** (`user_travel_memory`), **session** (clicks/skips in the last few minutes, from Redis). Separating "what they said" from "what we inferred" lets explicit always win (see `memory_grounding()` contract: never override explicit constraints).
- **③ Candidate Retrieval** — two parallel sources: (a) **live scrapers** for real prices/availability, (b) the **`hotels` master table** for enrichment + hotels the scrapers missed. Union, dedup by identity. (No vector ANN here — semantics enter at ⑥ as a *re-rank* over the already-scored top-50, not as a recall stage over the whole corpus; with only ~4k hotels, deterministic scoring is a perfectly good recall filter and far cheaper.)
- **④ Hard Filtering** — non-negotiable constraints (dates, pax, max budget ceiling, sold-out, wrong city, refundable-only). Cheap boolean gate *before* the expensive stages so we never score garbage. Filters that would empty the set are relaxed with a logged reason (see §4.3).
- **⑤ Feature Scoring (L2) → top-50** — the deterministic composite in `scoring.py`, run on **every** survivor. Fast, explainable, debuggable, unit-testable, works with zero LLM. This is the backbone *and* the recall filter: it cuts the field to the top 50 before anything expensive runs. If L3 (or Vertex) is down, L2 alone still ships a good answer.
- **⑥ Semantic Re-rank → top-15** — over the **top-50 only**, blend in cosine similarity between the **traveller-profile embedding** and each **hotel embedding** (Part 6). This is what makes "planned by someone who knows me" true. It's one *feature* mixed into the score (`0.75·L2 + 0.25·semantic`), not the whole ranking — pure vector search recommends irrelevant-but-similar junk. Cuts to top-15.
- **⑦ LLM Re-rank (L3) → top-1** — `recommendation_agent.py`. Sees only the top ≤ 8 (index-preserving prefix). Handles the last-mile human judgment L2 can't encode ("₹800 more but lands in daylight and free cancellation → worth it"). Streamed so the user reads the reason as it generates. **Optional by construction** — `None` return → L2/semantic order stands.
- **⑧ Explanation** — every shipped item carries a `why` (Part 15). For the hero pick it comes from L3; for the rest, deterministic from `why_tags`. Never expose raw scores.
- **⑨ Learn** — every impression/click/skip/book is logged to `rec_feedback` (Part 9), and trip completion runs `distill_from_trip()`. Off the hot path entirely (fire-and-forget to a background task / Celery).

### 1.3 Latency budget (P95, hotels example)

| Stage | Budget | Notes |
|---|---:|---|
| Intent (cache hit) | 0 ms | ~85% of repeat queries |
| Intent (cache miss) | 400 ms | `muscle` tier, `max_output_tokens=384` |
| Profile assembly | 15 ms | 1 Postgres row + 1 Redis GET |
| Retrieval — scrapers | 2–8 s | **the floor**; parallel `asyncio.gather`, already the case |
| Retrieval — vector ANN | 20 ms | HNSW, top-200 |
| Hard filter | 5 ms | in-memory |
| Semantic + L2 score | 40 ms | vectorized numpy over ≤ 300 items |
| L3 re-rank (streamed) | 1–2 s | overlaps nothing; first token ~500 ms |
| **User-perceived to first ranked list** | **≈ scrape time + 60 ms** | L3 streams *after* the list is already interactive |

The single most important architectural rule: **the ranked list renders from L2 the
instant scrapers return; L3 upgrades the hero pick's explanation asynchronously via
SSE.** We never make the user wait on the LLM to see results.

---

## Part 2 — Traveller Understanding Engine

### 2.1 Output schema (`TripIntent` + `TravellerProfile`)

Two objects. `TripIntent` is **per-request** (this trip). `TravellerProfile` is
**durable** (extends `user_travel_memory`). They are merged at ranking time.

```jsonc
// TripIntent — extracted from the current conversation/intake, per request
{
  "trip_purpose":   {"value": "honeymoon", "confidence": 0.82},   // leisure|business|honeymoon|family|workcation|backpacking|pilgrimage|event|relocation
  "traveller_type": {"value": "couple", "confidence": 0.9},        // solo|couple|family|friends|business
  "group_size": 2,
  "has_children": false,
  "budget_tier":    {"value": "mid", "confidence": 0.6},           // low|mid|high  (explicit intake overrides)
  "price_sensitivity":  {"value": 0.7, "confidence": 0.7},         // 0..1  how much price dominates
  "value_sensitivity":  {"value": 0.5, "confidence": 0.5},         // 0..1  deal-seeking vs indifferent
  "risk_tolerance":     {"value": 0.3, "confidence": 0.4},         // 0..1  low ⇒ wants refundable/flexible
  "vibes": [                                                        // free-form, embedded (Part 6)
    {"tag": "beachfront", "confidence": 0.8},
    {"tag": "relaxation", "confidence": 0.75},
    {"tag": "romantic",   "confidence": 0.7}
  ],
  "importance": {                                                   // 0..1 per axis → becomes ranking weights
    "cleanliness": 0.6, "convenience": 0.7, "cancellation": 0.4,
    "location": 0.9, "food": 0.5, "nightlife": 0.2, "quiet": 0.7,
    "photogenic": 0.6, "accessibility": 0.0
  },
  "time_prefs": {"depart_daypart": "morning", "avoid_redeye": true},
  "preferred_airlines": [], "preferred_hotel_brands": [],
  "hard_constraints": {                                             // → Part 4 hard filter
    "max_total_budget": 40000, "must_refundable": false,
    "date_from": "2026-08-03", "date_to": "2026-08-07", "pax": 2
  },
  "ambiguities": ["budget stated as 'not too much' — inferred mid, low confidence"]
}
```

`TravellerProfile` = the existing `user_travel_memory.memory` JSONB, **plus** a
structured `axes` block and a `profile_embedding` (Part 6). No new table needed for
the JSONB — we add columns to the existing one (Part 12).

### 2.2 How extraction works

Single `muscle`-tier call via `generate_json()` with a strict `response_schema`
(Vertex supports typed schemas — we already use this in `user_memory._DISTILL_SCHEMA`).
Prompt in Part 13.1. Rules:

1. **Confidence on everything.** The LLM emits `confidence ∈ [0,1]` per field. Low
   confidence ⇒ the field contributes proportionally less weight downstream
   (`effective_weight = base_weight × confidence`), so a guessed vibe never
   dominates a stated one.
2. **Explicit beats inferred.** Anything the user set in the structured intake
   (`workspace-launch.tsx` destination/origin/budget/who/vibe) is injected as
   `confidence: 1.0` and the LLM is told it may not contradict it.
3. **Ambiguity is surfaced, not hidden.** `ambiguities[]` is logged and can drive a
   clarify question (the copilot already has a clarify path — memory: "clarify").
4. **Cheap + cached.** Keyed `sha1(normalized_conversation)` → Redis 24 h. Re-runs of
   the same chat cost nothing.

### 2.3 Merge into ranking context

```python
def build_ranking_context(user_id, intent: TripIntent, session_signals) -> dict:
    memory = await get_memory(user_id)          # existing user_memory.py
    profile_axes = _profile_axes(memory)         # learned axes (Part 9)
    return {
        # importance weights: explicit intent, backfilled by learned axes, then priors
        "weights": _resolve_weights(intent.importance, profile_axes, COLD_START_PRIOR),
        "budget": intent.budget_tier.value,       # feeds recommendation_agent budget_rule
        "price_sensitivity": intent.price_sensitivity.value,
        "vibes": [v.tag for v in intent.vibes],
        "profile_embedding": profile_axes.embedding,   # Part 6
        "traveller_memory": memory_grounding(memory),  # existing prose grounding
        "session": session_signals,               # last-N clicks/skips from Redis
        "hard": intent.hard_constraints,
    }
```

This dict is exactly the `context` already threaded into
`recommendation_agent._recommend()` — we are enriching it, not replacing it.

---

## Part 3 — Hotel Intelligence (semantic profiles)

Every hotel becomes an **AI profile** on top of its scraped fields. Two artifacts:

1. **`hotel_profiles` row** (durable, one per master-table hotel) — the SEO page +
   the reusable semantic understanding. Generated **offline** by a background worker
   the first time a hotel is seen and refreshed on a TTL. **Never on the hot path.**
2. **Personality tags** — a controlled vocabulary of archetypes, assigned by rules +
   one `muscle` LLM pass, stored on the profile and used as ranking features + chips.

### 3.1 Personality vocabulary (controlled, ~24 tags)

```
luxury_palace  heritage_royal  boutique_design  instagram_icon  hidden_gem
budget_explorer  backpacker_social  family_comfort  workcation_ready
lake_view  beachfront  mountain_view  city_central  nature_retreat
peaceful_retreat  party_hub  foodie_haven  wellness_spa  romantic_couples
business_express  pet_friendly  accessible  eco_conscious  long_stay
```

Controlled vocab (not free text) so tags are filterable, aggregatable, and
comparable across hotels. Free-form nuance lives in the embedding.

### 3.2 How a tag is assigned (rules → embedding-cluster → LLM only on low confidence)

**No per-hotel Gemini call.** Tagging is three cheap tiers; the LLM is the rare
exception, not the rule (≈ 90% cost cut vs an LLM-per-hotel approach):

1. **Deterministic rules** (below) fire the unambiguous tags from structured fields.
2. **Embedding → nearest personality cluster.** We embed the hotel's review/profile
   doc once (Part 6, 384-dim) and assign the fuzzy tags (`romantic_couples`,
   `beachfront`, `luxury_palace`, `workcation_ready`, `hidden_gem`, `party_hub`, …)
   by cosine to a small set of **precomputed cluster centroids** — one per
   personality, built once from labelled seed hotels. `tag = argmax cosine(hotel,
   centroid)`; assign every centroid above a similarity threshold.
3. **Gemini only when confidence is low** — i.e. the top cluster similarity is below
   `τ_low` (ambiguous hotel, no clear cluster). Then and only then run the `muscle`
   tag+summary pass. In practice this is a small minority of hotels.

**Cluster centroids** are cheap to build and maintain: embed ~15–30 hand-labelled
exemplar hotels per personality, mean-pool → centroid, store in a tiny
`personality_centroids` table. Re-fit occasionally offline; no LLM at serving time.

**Deterministic rules** (cheap, run always) derive the unambiguous tags from
already-scraped fields — extends the `why_tags` logic in `scoring.py`:

```python
def rule_tags(h: dict) -> set[str]:
    t = set()
    sub = h.get("subscores") or {}
    if (h.get("star_rating") or 0) >= 5 and (h.get("price_per_night") or 0) > P90_city: t.add("luxury_palace")
    if any("heritage" in x.lower() or "palace" in x.lower() for x in [h.get("name",""), h.get("property_type","")]): t.add("heritage_royal")
    if (sub.get("location") or 0) >= 8.5 and (h.get("distance_km") or 9) <= 1.5: t.add("city_central")
    if h.get("price_per_night",1e9) <= P25_city and (h.get("review_score") or 0) >= 7.5: t.add("budget_explorer")
    if "hostel" in (h.get("property_type","")).lower(): t.add("backpacker_social")
    if h.get("family_friendly") or (h.get("group_size_ok") and h.get("has_kitchen")): t.add("family_comfort")
    if h.get("free_wifi") and h.get("has_workspace"): t.add("workcation_ready")
    for kw, tag in [("lake","lake_view"),("beach","beachfront"),("mountain","mountain_view")]:
        if any(kw in (l.get("name","")).lower() for l in (h.get("landmarks") or [])): t.add(tag)
    if (sub.get("cleanliness") or 0) >= 9: t.add("wellness_spa") if h.get("has_spa") else None
    return {x for x in t if x}
```

**Embedding-cluster pass** (default, no LLM) assigns the *fuzzy, review-driven* tags
(`hidden_gem`, `romantic_couples`, `peaceful_retreat`, `foodie_haven`,
`instagram_icon`, `party_hub`) by cosine to the personality centroids. **LLM pass**
(`muscle`, offline) runs **only** when the best cluster similarity < `τ_low` — it
produces the tags + `one_liner`/`seo_summary` for the ambiguous minority. Prompt in
Part 13.2. Output constrained to the vocab via `response_schema` enum.

### 3.3 `hotel_profiles` content (the SEO artifact + the ML features)

```jsonc
{
  "hotel_id": "uuid (FK → hotels.id)",
  "personality_tags": ["romantic_couples", "lake_view", "boutique_design"],
  "one_liner": "A lake-facing boutique stay where nearly every review mentions the sunset terrace.",  // SEO H1/meta
  "seo_summary": "180–300 word original prose for the public /hotels/<slug> page",     // SEO body
  "highlights": ["Rooftop lake view", "Walkable to old city (600 m)", "Free cancellation"],
  "best_for": ["couples", "photographers", "slow travellers"],
  "not_for":  ["nightlife seekers", "large families"],
  "vibe_vector_ready": true,                 // has an embedding row
  "confidence": 0.78,
  "generated_by": "muscle:gemini-2.5-flash-lite",
  "source_review_count": 214,                // how much evidence backed this
  "refreshed_at": "2026-07-13T..."
}
```

The `one_liner` / `seo_summary` **directly satisfy the "individual profile for SEO"
requirement** — each master-table hotel gets a public, indexable page (Part 12.3).

---

## Part 4 — Transportation Intelligence

Same pattern, different vocab. Applies to flights, trains, buses uniformly.

### 4.1 Transport personality vocabulary (~16 tags)

```
budget_hero  best_value  fast_escape  nonstop_premium  red_eye
comfort_pick  business_express  family_friendly  backpacker_choice
scenic_route  sleeper_champion  weekend_winner  early_bird  late_departure
generous_baggage  flexible_fare
```

### 4.2 Assignment — mostly deterministic (extends `scoring.py::score_flights` tags)

Transport is structured enough that ~90% is rules; almost no LLM needed:

```python
def transport_tags(x: dict, ctx: SetStats) -> set[str]:
    t = set()
    if x["price"] <= ctx.p10: t.add("budget_hero")
    if x.get("rank_score",0) >= ctx.rank_p90: t.add("best_value")
    if x.get("stops",0) == 0 and x["mode"]=="flight": t.add("nonstop_premium")
    if x["duration_min"] <= ctx.dur_p10: t.add("fast_escape")
    dh = _dep_hour(x)
    if dh is not None and (dh >= 22 or dh < 5): t.add("red_eye")
    if dh is not None and dh < 8: t.add("early_bird")
    if (x.get("baggage_kg") or 0) >= 20: t.add("generous_baggage")
    if x.get("fare_type","").lower() in {"flexi","flex","refundable"} or x.get("is_refundable"): t.add("flexible_fare")
    if x["mode"]=="train" and x.get("class") in {"SL","2S"}: t.add("sleeper_champion")
    if x["mode"]=="train" and x.get("scenic"): t.add("scenic_route")
    if x["mode"]=="bus" and x.get("class","").lower().find("sleeper")>=0: t.add("comfort_pick")
    return t
```

`fare_tags` from Gotogate (`["PRICE","WEIGHTED"]`) map directly:
`PRICE→budget_hero`, `WEIGHTED→best_value`. This is why we enriched the parsers.

### 4.3 Hard-filter relaxation (the Bhiwani→Rishikesh "no train/bus" bug, generalized)

The transport recommender must **never return an empty set silently.** Filtering is
tiered; if a tier empties the candidate pool, drop the softest constraint and log:

```python
FILTER_TIERS = [
  ("sold_out",        lambda x: x.available),          # never relax
  ("wrong_od",        lambda x: x.o==O and x.d==D),     # never relax
  ("budget_ceiling",  lambda x: x.price <= cap*1.15),   # relax to 1.5×, then drop
  ("refundable_only", lambda x: x.refundable),          # relax if empties
  ("daypart",         lambda x: x.dep in window),       # relax first
]
def hard_filter(cands, tiers):
    for relax_level in range(len(tiers)+1):
        active = tiers[:len(tiers)-relax_level] if relax_level else tiers
        kept = [c for c in cands if all(pred(c) for _,pred in active)]
        if kept: 
            if relax_level: log.info("relaxed %s", [n for n,_ in tiers[len(tiers)-relax_level:]])
            return kept, active
    return cands, []   # last resort: show everything, flag it
```

This structurally prevents "showed neither train nor bus": a route that has *any*
option always renders one, with a note if constraints were relaxed.

---

## Part 5 — Journey Intelligence (the differentiator)

TravBuddy ranks **journeys**, not legs. A journey = `{outbound transport, stay,
return transport}` over the trip graph. Extends `journey_reasoning.py`.

### 5.1 Journey Score

For a candidate journey *J* built from transport option *t*, stay *h* over *n* nights:

```
JourneyScore(J) =  w_cost   · CostFit(J)
                +  w_time   · TimeQuality(J)
                +  w_stay   · h.rank_score/100
                +  w_arr    · ArrivalQuality(t)      // daytime arrival, transfer ease
                +  w_dep    · DepartureQuality(t)
                +  w_fatigue· (1 − Fatigue(J))       // legs, layovers, red-eyes, total transit h
                +  w_ctx    · ContextFit(J)           // weather/crowd/season at destination
                −  penalties(J)                       // hidden hotel-night, tight connection, self-transfer
```

Key coupling terms (why leg-by-leg optimization is wrong):

- **Hidden hotel-night** — already modeled in `journey_reasoning._is_late_arrival`.
  A ₹1,200-cheaper flight landing 02:10 costs a real hotel night → fold `+hotel_per_night`
  into that journey's effective cost. Make it a *score penalty*, not just prose.
- **Arrival↔check-in coupling** — a stay with free/late check-in absorbs a late
  arrival; pair-aware, not per-item.
- **Transfer friction** — airport→hotel distance × arrival daypart (a 40 km transfer
  at 2 AM is a different journey than at 2 PM even with identical legs).
- **Fatigue** — `Fatigue = σ(a·total_transit_h + b·num_legs + c·redeye + d·layover_h)`.
  Backpackers tolerate it (low `w_fatigue`); families/honeymooners don't (high).

### 5.2 Weights are contextual (Part 8 table). Honeymoon: `w_stay↑ w_fatigue↑ w_cost↓`.
Backpacker: `w_cost↑ w_fatigue↓`. Business: `w_time↑ w_arr↑ w_cost↓`.

### 5.3 Multi-city journeys — score each leg's journey, then the chain

For a graph with stops `[A→B→C→A]`, score each leg-journey independently, then add a
**chain coherence** term: reward consistent arrival/departure rhythm, penalize
back-tracking (the return-mirror work already reverses the outbound chain — the score
rewards that symmetry). Emit per-leg `why` + one trip-level headline
(`journey_reasoning.build_journey_reasoning` already produces this shape).

---

## Part 6 — Embedding Strategy

**Hotels yes, travellers yes, journeys yes — transport/flights NO.** A flight is
`{origin, destination, price, stops, duration, baggage}`: fully structured, and rules
+ features (Parts 4, 7) capture everything an embedding would. Embedding it adds cost
and RAM for ~zero ranking signal, so we **do not embed transport at all.**

**384 dimensions, not 768.** We are embedding ~4,000 hotels, not 100M documents.
384-dim (`text-embedding` at reduced output dim, or a 384-dim model) is cheaper,
faster, and uses half the RAM/index size with no measurable recall loss at this
corpus scale.

| Entity | Embed? | Dim | Why | When |
|---|---|---|---|---|
| **Hotel** | ✅ | 384 | semantic re-rank + personality clustering (Part 3) + "similar hotels" SEO links | Phase 1 |
| **Traveller profile** | ✅ | 384 | the "knows me" vector; cosine vs hotel = personalization core | Phase 1 |
| **Transport / flight** | ❌ | — | structured; rules + features capture it fully — embedding adds nothing | never |
| **Journey (finished trip)** | ⚠️ | 384 | "trips like this" discovery + cold-start priors; embed the *completed* trip for the learning loop | Phase 3 |

### 6.1 What text gets embedded (the document builder)

Embedding quality is 90% about the document you build. For a hotel:

```
"{name}. {property_type} in {area}, {city}. {star}★, guest score {review_score}/10
 ({review_label}). Personality: {personality_tags joined}. {one_liner}.
 Near: {top 3 landmarks}. {distance_km} km from centre. Strong on: {top-2 subscores}.
 Guests say: {review_snippet}. Amenities: {top 8}. {'Free cancellation.' if flex}"
```

Rules:
- **Embed review snippets** — yes, they carry the vibe keywords ("perfect for
  couples", "loud at night") nothing else has. But embed a *distilled* snippet, not
  raw reviews (noise). The `hotel_profiles.one_liner` + `seo_summary` are ideal
  embed inputs (already denoised).
- **Embed landmarks** — yes, as names in the doc ("Near Lake Pichola, City Palace").
  Enables "hotel near the ghats" style recall.
- **Do NOT embed price/dates/availability** — volatile; those are filters/features,
  not semantics. Embedding them poisons the vector on every price change.
- **Traveller doc** = `memory_grounding(memory)` prose ⊕ current `intent.vibes` ⊕
  `trip_purpose`. Rebuilt per request (cheap) but the *durable* part is cached.
- **Conversation history** — embed a rolling *summary* (the copilot already
  summarizes), not raw turns.

### 6.2 Model + storage

- **Model:** Vertex `text-embedding-005` with `output_dimensionality=384` (or a native
  384-dim model), via a new `app/services/embeddings.py` (`embed_texts(list[str]) ->
  list[vec]`, batched, retried, same region logic as `vertex.py`). Normalize to unit
  length → cosine = dot.
- **Storage:** Postgres `pgvector`, `vector(384)`, **HNSW** index
  (`m=16, ef_construction=64`), `vector_cosine_ops`. Tables in Part 12.
- **Never embed on the hot path.** A Celery worker embeds new hotels when
  `_hotel_master_rows` upserts them (enqueue hotel_ids). Traveller-profile embedding
  is cheap enough to compute per session and cache 24 h in Redis.
- **Re-embed only when the doc changes.** Store `doc_hash` on the embedding row; on
  refresh, rebuild the doc, hash it, and **skip** the embedding call if the hash is
  unchanged (see §6.3). Same gate governs profile regeneration.

### 6.3 Regenerate profiles/embeddings only on real change

Never regenerate a hotel's profile or embedding on a schedule. The signal that a
profile is stale is a change in the *evidence* it was built from:

```python
def profile_stale(hotel, existing_profile) -> bool:
    sig = sha1(f"{hotel.review_count}|{hotel.review_score}|{hash(hotel.description)}")
    return existing_profile is None or existing_profile.doc_hash != sig
    #        regenerate ONLY if review_count / rating / description changed
```

The refresh worker computes `sig` and, if it matches the stored `doc_hash`, **skips**
both the LLM/cluster tag pass and the embedding call. For ~4k hotels that mostly sit
still between scrapes, this makes steady-state profiling cost approach zero — we only
pay when a hotel actually gained reviews or changed its rating/description.

---

## Part 7 — Feature Engineering

Every feature is a pure function → 0..1, unit-tested, logged. These extend the
components already in `scoring.py`. Formulas:

| Feature | Formula (→ 0..1) | Notes |
|---|---|---|
| **BudgetFit** | `low`: `clip(cheapest/price)`; `mid`: `1−|price−median|/median`; `high`: `clip(price/p90)` capped | already the low-path in `helpers.rank_hotels`; generalize per tier |
| **PriceFairness** | `1 − clip((price − expected(area,star,season))/expected)` | needs `expected` model (Part 9 offline); until then use set median |
| **DealQuality** | `clip(discount_pct/40) · min(1, savings/2000) · is_real_discount` | uses enriched `savings`/`discount_pct`; `is_real_discount` guards fake crossed-out |
| **ReviewConfidence** | `(rating/10) · log1p(count)/log1p(500)` | shrinks a 9.8 from 3 reviews; extends `_log_scale` |
| **SemanticMatch** | `cosine(profile_emb, item_emb)` rescaled `(c+1)/2` | Part 6 |
| **Convenience** | `w1·(1−dist/dmax) + w2·transit_access + w3·central` | hotel proximity |
| **ArrivalQuality** | daytime→1, evening→0.7, red-eye→0.2 | `journey_reasoning._arrival_hour` |
| **DepartureQuality** | Gaussian around intent daypart | extends `_dep_convenience` |
| **LayoverQuality** | `1` nonstop; else `1 − clip(layover_h/6) − 0.3·self_transfer` | uses `layover_airports`, `self_transfer` |
| **JourneyFatigue** | `σ(a·transit_h + b·legs + c·redeye + d·layover_h)` | Part 5 |
| **HotelPersonalityFit** | `Jaccard(personality_tags, intent.vibes∪purpose_tags)` | Parts 3+2 |
| **TransportPersonalityFit** | same, transport vocab | Part 4 |
| **Comfort** | flights: `f(cabin, baggage, seat)`; hotels: `subscores.facilities/10`; trains: class rank | |
| **FlexibilityFit** | `free_cancellation·risk_sensitivity` | `risk_tolerance` low ⇒ weight up |
| **Freshness** | `0.5·has_live_price + 0.3·flexible + 0.2·available` | already in `scoring.py` |
| **Scarcity** | `available_rooms≤3 → 0.8; seats≤5 → 0.7` | urgency chip only, small weight |

**Absent-field-aware weighting (critical — from the Udaipur dump finding).** EMT
hotels have almost no signal; scoring an absent field as 0 unfairly buries them.
Instead, **renormalize weights over present features per item:**

```python
def weighted_present(components: dict, weights: dict) -> float:
    present = {k: w for k, w in weights.items() if components.get(k) is not None}
    if not present: return 50.0
    z = sum(present.values())
    return round(100 * sum(components[k] * (w/z) for k, w in present.items()), 1)
```

So a hotel with only price+rating is scored on price+rating (reweighted to sum 1),
not penalized for missing subscores. Pair this with **cross-provider merge**
(§8.2) so the richest provider's metadata attaches to the cheapest provider's price.

---

## Part 8 — Recommendation Algorithms

### 8.1 Unified hybrid score

Same skeleton for hotels/flights/trains/buses/journeys; only the feature set +
weights differ:

```
FinalScore(item) = 100 · Σ_f  weight_f(context) · confidence_adj · feature_f(item)
                   ─────────────────────────────────────────────────────────────
                                 Σ_f present weight_f            (renormalized, §7)

FinalScore ← FinalScore · provider_confidence            // scoring.py _*_PROVIDER_CONFIDENCE
FinalScore ← blend(FinalScore, SemanticMatch)            // 0.75·feat + 0.25·semantic (tunable)
```

Then L3 (`recommendation_agent`) re-ranks the **top-8** and picks the hero.

### 8.2 Hotels algorithm (extends `score_hotels`)

```python
def recommend_hotels(raw, context):
    cands = dedup_cross_provider(raw)          # ① merge same hotel across providers,
                                               #    keep min price, richest metadata
    cands = [h for h in cands if hard_ok(h, context.hard)]     # ② Part 4 tiered filter
    attach_embeddings(cands)                   # ③ from hotel_embeddings (cached)
    for h in cands:
        h["features"] = hotel_features(h, context)             # Part 7
        h["final"] = weighted_present(h["features"], HOTEL_WEIGHTS[context.persona])
        h["final"] = blend(h["final"], semantic_match(h, context.profile_embedding))
        h["personality_tags"] = profile_tags(h)                # Part 3 (cached)
        h["why_tags"] = derive_why(h, cands)                   # scoring.py, extended
    cands.sort(key=lambda h: h["final"], reverse=True)
    hero = await llm_rerank_hotels(cands[:8], context)         # recommendation_agent, budget-aware
    return assemble(cands, hero)
```

`dedup_cross_provider` keyed on `(normalized_name, round(lat,3), round(lon,3))` or
master-table `hotel_id` — solves the "same Udaipur hotel bare in EMT, rich in Agoda"
problem from the dump analysis.

### 8.3 Transport algorithm (extends `score_flights`, adds trains/buses)

Identical shape; features = price, duration, stops/directness, layover_quality,
baggage, departure/arrival quality, fare flexibility, provider_confidence, personality
fit. **Cross-mode** for a route (flight vs train vs bus): normalize each mode's score
within-mode, then compare on **JourneyScore** (Part 5) so a 2h flight and a 12h
sleeper train are compared on total value, not raw duration.

### 8.4 Contextual weights (the "knows me" knob)

`HOTEL_WEIGHTS[persona]` and `TRANSPORT_WEIGHTS[persona]` — persona derived from
`trip_purpose × traveller_type`. Illustrative hotel weights (must sum ≈ 1 before
renorm):

| persona | price | rating | location | personality | flexibility | comfort | semantic |
|---|---:|---:|---:|---:|---:|---:|---:|
| **backpacker/solo** | .32 | .14 | .18 | .12 | .06 | .04 | .14 |
| **budget/family** | .30 | .18 | .14 | .08 | .10 | .08 | .12 |
| **weekend/friends** | .22 | .16 | .20 | .12 | .06 | .06 | .18 |
| **honeymoon/couple** | .10 | .20 | .16 | .18 | .06 | .14 | .16 |
| **luxury** | .06 | .22 | .16 | .16 | .06 | .18 | .16 |
| **business** | .14 | .18 | .24 | .06 | .12 | .14 | .12 |
| **workcation** | .18 | .16 | .16 | .14 (workcation_ready) | .10 | .12 | .14 |

Transport analog (price/duration/stops/arrival/baggage/flexibility/comfort) with
business→`duration↑ arrival↑`, backpacker→`price↑ fatigue↓`, family→`comfort↑ stops↓`.
Weights live in a module dict like today's `HOTEL_RANK_WEIGHTS` so they're tunable
without code changes; later moved to a `ranking_weights` config table for
experiment-driven updates (Part 9).

---

## Part 9 — Learning System

### 9.1 Signals → `rec_feedback` event log (Part 12)

Every user action emits one row (fire-and-forget). Impressions included so we can
compute CTR and train a real model later:

```
impression, click, wishlist_add, wishlist_remove, book_start, book_complete,
skip (scrolled past N above it), dwell_ms, compare_open, cancel, review_submit,
trip_complete
```

### 9.2 Online learning (fast loop, minutes)

- **Session re-ranking:** clicks/skips in the current session update a per-session
  bias vector in Redis (`sess:{id}:bias`) — e.g. user keeps opening beachfront →
  bump `beachfront` vibe weight for the rest of the session. Bounded, decays, never
  persisted unless it survives to `distill_from_trip`.
- **Bandit on weight sets:** treat each persona's weight vector as an arm; Thompson
  sampling on `book_complete` reward to pick between weight variants. Start with 2–3
  variants per persona; the winner updates `ranking_weights`.

### 9.3 Offline learning (slow loop, nightly Celery)

- **`distill_from_trip()`** already exists — keep. Extend to also write structured
  `axes` (learned importance per axis) into `traveller_profiles`.
- **PriceFairness model:** nightly job aggregates `transport_options` + hotel history
  → `expected_price(area, star, season, dow)` table. Feeds Part 7 `PriceFairness`.
- **User embedding:** nightly, embed the union of a user's booked/wishlisted item docs
  → `traveller_profiles.profile_embedding` (collaborative signal: "users like you").
- **Popularity priors:** aggregate `rec_feedback` → per-item CTR/CVR, EWMA-decayed,
  into the feature store (a mild prior, not a dominant term — avoid rich-get-richer).

### 9.4 User embeddings — two sources, blended

`profile_embedding = 0.6·explicit_profile_emb + 0.4·behavioral_emb` where behavioral
= mean of embeddings of items they engaged with (recency-weighted). Blended so a new
stated preference isn't drowned by history and vice-versa.

---

## Part 10 — Cold Start

No history, no bookings, no profile. Strategy = **priors + fast active learning.**

1. **Destination-persona priors.** Precompute, per destination, the modal traveller
   personas + top personality tags from *all* users' finished trips (`journeys` +
   `rec_feedback`). New user to Udaipur → seed `vibes=[romantic, heritage, lake_view]`
   because that's what Udaipur trips look like. Stored `destination_priors` table.
2. **Intake is the cold-start solver.** The `workspace-launch.tsx` intake (who/vibe/
   budget/when) gives a usable `TripIntent` on turn one — cold start is mostly solved
   by *asking*, cheaply, which we already do.
3. **Popularity-with-diversity fallback.** Before any signal, rank by
   `quality_score` (absolute, price-independent — already in `scoring.py`) + MMR
   diversity so the first screen shows range (one budget, one mid, one splurge, one
   central, one scenic) rather than five near-identical hotels.
4. **Fast active learning.** First click/skip immediately shifts the session bias
   (§9.2). By item 3 the list is already personalizing.
5. **Global prior embedding** for `SemanticMatch` = centroid of the chosen budget
   tier's booked hotels, until the user has their own vector.

---

## Part 11 — Recommendation APIs

All under `/api/v1/recommend`. Auth = existing `app/deps/auth.py`. Responses **never
include raw scores** (Part 15). Backward-compatible with today's
`/search/*/smart` (which already returns `recommended` + `reason`).

### 11.1 `POST /recommend/hotels`

```jsonc
// Request
{
  "trip_id": "uuid|null",
  "destination": "Udaipur",
  "checkin": "2026-08-03", "checkout": "2026-08-07", "pax": 2,
  "intent_override": { /* partial TripIntent from intake; server merges */ },
  "candidates": [ /* optional: pre-fetched scraper hotels; else server scrapes */ ],
  "limit": 20, "explain": true
}
// Response
{
  "hero": {
    "hotel_id": "uuid", "name": "Lake Pichola Hotel",
    "price_per_night": 7400, "currency": "INR",
    "why": "Lake-facing and 600 m from the old city — the romantic base your trip is built around, and it's ₹3,342 off tonight.",
    "why_tags": ["Lake view", "Walkable centre", "Save ₹3,342", "Free cancellation"],
    "personality_tags": ["romantic_couples","lake_view","boutique_design"],
    "best_for": ["couples","photographers"], "not_for": ["nightlife seekers"],
    "tradeoff": "₹900/night over the cheapest, but on the lake and refundable.",
    "match": "high"                    // bucketed, never a number
  },
  "alternatives": [ { "hotel_id": "...", "label": "Cheapest that's still central", "why": "..." }, ... ],
  "results": [ /* full ranked list, each with why_tags + match bucket */ ],
  "relaxations": [],                   // e.g. ["widened budget to ₹8k — nothing under ₹6k on the lake"]
  "intent_echo": { /* resolved TripIntent so the client can show/adjust */ }
}
```

### 11.2 `POST /recommend/transport`

Request adds `origin`, `modes:["flight","train","bus"]`, `date`, `sort`. Response
mirrors above with `hero` + per-mode `best` + journey-aware `tradeoff`
(uses `journey_reasoning`).

### 11.3 `POST /recommend/journey`

The headline endpoint. Request = full trip graph (stops + legs) or `trip_id`.
Response = per-leg transport hero + stay hero + **trip-level `journey_score`
narrative** (`journey_reasoning.build_journey_reasoning` shape) + total cost + the
return-mirror. Streamed via SSE (reuse `app/core/sse.py`): emit L2 list first, then
L3 hero explanations as they generate.

### 11.4 `POST /recommend/explain`

`{item_id | item, context}` → `{why, tradeoffs, best_for, not_for, alternatives}`.
Powers the copilot "why did you pick X" answer (fixes the contradiction bug — same
grounding as ranking, so the explanation can't disagree with the pick). Cached on
`(item_id, profile_hash)`.

### 11.5 `POST /recommend/feedback`

`{item_id, event, trip_id, position, dwell_ms}` → `204`. Fire-and-forget to
`rec_feedback`. Never blocks UI.

---

## Part 12 — Database Design

Postgres (Supabase) + pgvector + Redis. Extends existing tables; new tables marked
**NEW**. All timestamps `timestamptz default now()`.

### 12.1 Extend `hotels` (master identity — exists)

Already has `source, source_hotel_id, name, address, city, destination, lat, lon,
star_rating, review_score, review_count, review_label, property_type, amenities,
photos, booking_url, map_url, description, quality_score, last_seen_at`. Add:

```sql
ALTER TABLE hotels ADD COLUMN IF NOT EXISTS slug text UNIQUE;          -- SEO url
ALTER TABLE hotels ADD COLUMN IF NOT EXISTS subscores jsonb;           -- enriched Agoda subscores
ALTER TABLE hotels ADD COLUMN IF NOT EXISTS landmarks jsonb;
ALTER TABLE hotels ADD COLUMN IF NOT EXISTS canonical_hotel_id uuid;   -- cross-provider merge target
CREATE INDEX IF NOT EXISTS hotels_dest_idx ON hotels (destination);
CREATE INDEX IF NOT EXISTS hotels_geo_idx  ON hotels (round(lat::numeric,3), round(lon::numeric,3));
```

### 12.2 `hotel_profiles` **NEW** (SEO + semantic)

```sql
CREATE TABLE hotel_profiles (
  hotel_id          uuid PRIMARY KEY REFERENCES hotels(id) ON DELETE CASCADE,
  personality_tags  text[]   NOT NULL DEFAULT '{}',
  one_liner         text,                 -- SEO H1 / meta description
  seo_summary       text,                 -- 180–300 word indexable body
  highlights        text[]   DEFAULT '{}',
  best_for          text[]   DEFAULT '{}',
  not_for           text[]   DEFAULT '{}',
  confidence        real,
  source_review_count int,
  generated_by      text,
  refreshed_at      timestamptz DEFAULT now()
);
CREATE INDEX hotel_profiles_tags_idx ON hotel_profiles USING gin (personality_tags);
```

### 12.3 Public SEO surface

Frontend renders `/hotels/<slug>` from `hotels ⋈ hotel_profiles`. `slug =
slugify(name)+'-'+city+'-'+short(hotel_id)`. Sitemap generated nightly from
`hotels` where `hotel_profiles.seo_summary is not null`. Internal links = top-5
cosine-nearest hotels (Part 6) → link graph for SEO + "similar stays".

### 12.4 Vector tables **NEW** (pgvector)

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE hotel_embeddings (
  hotel_id  uuid PRIMARY KEY REFERENCES hotels(id) ON DELETE CASCADE,
  embedding vector(384) NOT NULL,
  doc_hash  text,                    -- skip re-embed if unchanged (§6.3)
  model     text,
  updated_at timestamptz DEFAULT now()
);
CREATE INDEX hotel_emb_hnsw ON hotel_embeddings
  USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);

CREATE TABLE personality_centroids (            -- Part 3.2 — one row per personality tag
  tag       text PRIMARY KEY,
  centroid  vector(384) NOT NULL,
  n_exemplars int,
  updated_at timestamptz DEFAULT now()
);

CREATE TABLE traveller_profiles (               -- extends user_travel_memory
  user_id   uuid PRIMARY KEY,
  axes      jsonb,                    -- learned importance per axis (Part 9.3)
  embedding vector(384),
  updated_at timestamptz DEFAULT now()
);
-- NO transport_embeddings — flights/trains/buses are not embedded (Part 6).
-- journey_embeddings: Phase 3 only, vector(384), finished-trip docs.
```

### 12.5 `transport_options` **NEW** (the "save transport for training data" ask)

Every scraped flight/train/bus result is persisted — the future training corpus.
Written async off the hot path (like `_hotel_master_rows`), never blocks search.

```sql
CREATE TABLE transport_options (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  mode          text NOT NULL,              -- flight|train|bus
  source        text NOT NULL,              -- booking|easemytrip|gotogate|confirmtkt|redbus
  origin        text, destination text,     -- resolved codes/stations
  depart_date   date,
  operator      text, operator_code text,   -- airline/train#/bus operator
  service_no    text,                       -- flight_number / train_number
  depart_time   text, arrive_time text, duration_min int, stops int,
  price         numeric, currency text default 'INR',
  fare_type     text, baggage_kg int, cabin_baggage_kg int,
  layover_airports text[], seats_available int, refundable boolean,
  raw           jsonb,                       -- full normalized dict (parser output)
  scraped_at    timestamptz DEFAULT now()
);
CREATE INDEX transport_od_date_idx ON transport_options (mode, origin, destination, depart_date);
CREATE INDEX transport_scraped_idx ON transport_options (scraped_at);
```

Partition by month (`scraped_at`) once volume grows; `raw` is the ground truth for
retraining price models + a future learned ranker. This table is **the training data
flywheel** — every search makes the price-fairness model and demand curves better.

### 12.6 Feedback + learning tables **NEW**

```sql
CREATE TABLE rec_feedback (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid, trip_id uuid, session_id text,
  item_type text,          -- hotel|flight|train|bus|journey
  item_id text,            -- hotel_id or transport_options.id
  event text NOT NULL,     -- impression|click|wishlist|book_start|book_complete|skip|dwell|cancel
  position int, dwell_ms int,
  context jsonb,           -- persona, weights variant, query hash (for offline eval)
  created_at timestamptz DEFAULT now()
);
CREATE INDEX rec_feedback_user_idx ON rec_feedback (user_id, created_at);
CREATE INDEX rec_feedback_item_idx ON rec_feedback (item_type, item_id);

CREATE TABLE ranking_weights (            -- experiment-driven, replaces hardcoded dicts over time
  persona text, feature text, weight real, variant text DEFAULT 'control',
  updated_at timestamptz DEFAULT now(),
  PRIMARY KEY (persona, feature, variant)
);

CREATE TABLE expected_price (             -- nightly PriceFairness model (Part 9.3)
  scope text, key text, p10 numeric, p50 numeric, p90 numeric, n int,
  computed_at timestamptz DEFAULT now(), PRIMARY KEY (scope, key)
);

CREATE TABLE destination_priors (         -- cold start (Part 10)
  destination text PRIMARY KEY,
  top_personas text[], top_vibes text[], seed_embedding vector(768),
  updated_at timestamptz DEFAULT now()
);
```

### 12.7 Redis (feature store + cache)

| Key | Value | TTL | Purpose |
|---|---|---|---|
| `intent:{sha1(text,user)}` | TripIntent JSON | 24 h | skip re-extraction |
| `profile_emb:{user}` | 768 float32 blob | 24 h | avoid re-embedding profile |
| `rec:{query_hash}` | ranked list JSON | 10 min | identical query within a session |
| `sess:{sid}:bias` | vibe→delta hash | 30 min | online session re-rank (§9.2) |
| `explain:{item}:{profile_hash}` | why JSON | 1 h | copilot "why" answers |
| `weights:{persona}` | weight dict | 5 min | hot-loaded from `ranking_weights` |

Recommendation cache is invalidated on price refresh (short TTL is enough — prices
move; we accept 10-min staleness on ordering, never on displayed price which is
re-fetched live).

---

## Part 13 — Prompt Design

All via `app/services/vertex.py`. Tier noted per prompt. All use
`response_mime_type="application/json"` + a typed `response_schema` (Vertex enforces
shape — we already do this in `user_memory._DISTILL_SCHEMA`).

### 13.1 Intent Extraction (`muscle`, temp 0.2)

```
SYSTEM: You convert a traveller's words into a structured trip intent. Infer only
what the text supports. Put a confidence 0..1 on every field. Never contradict an
EXPLICIT field (marked given=true) — copy it with confidence 1.0. List anything
ambiguous in "ambiguities". Do not invent constraints the user didn't imply.

EXPLICIT (authoritative): {intake_json}          # destination/origin/budget/who/vibe/when
CONVERSATION: {chat_text}
LEARNED ABOUT THIS TRAVELLER (prior, may be overridden): {memory_grounding}

Return JSON matching the TripIntent schema. For "importance", output 0..1 per axis
(location, cleanliness, convenience, cancellation, food, nightlife, quiet,
photogenic, accessibility). For price_sensitivity/value_sensitivity/risk_tolerance
output 0..1 with confidence.
```

### 13.2 Hotel personality tagging (`muscle`, temp 0.3, offline)

```
Assign personality tags to this hotel from the CONTROLLED VOCAB only:
[luxury_palace, heritage_royal, ... long_stay].  Pick 2–5 that the evidence
supports. Base them on reviews + subscores + type, NOT price alone. Then write:
- one_liner: ≤ 22 words, specific, no marketing fluff, quote what guests actually praise.
- seo_summary: 180–300 words, original, factual, third person, for a public page.
- best_for / not_for: 2–3 traveller types each.
EVIDENCE: name, type, area, star, subscores{...}, review_label, review_snippet,
landmarks[...], amenities[...], social_proof, distance_km.
Return JSON {personality_tags[], one_liner, seo_summary, highlights[], best_for[],
not_for[], confidence}. If evidence is thin, lower confidence and stay generic —
never fabricate a view, an award, or a guest quote.
```

### 13.3 Hotel/Transport recommendation (`brain`, temp 0.3) — **exists**, keep

`app/agents/recommendation_agent.py::_recommend` is production-ready. Keep its
budget_rule + memory injection + strict JSON. Change: feed the new
`personality_tags` + `match` bucket into `_compact_hotel/_compact_flight`, and add
`tradeoff` to `_SCHEMA_HINT`.

### 13.4 Journey recommendation (`brain`, temp 0.3)

```
You are TravBuddy's journey planner. Given transport options per leg and stays for
each stop, choose a coherent END-TO-END journey, not the cheapest of each part.
Weigh: hidden hotel-night from late arrivals, transfer friction, total fatigue,
and the traveller's persona {persona}. Prefer a journey where arrivals land in
daylight and the return mirrors the outbound. Reference real numbers.
LEGS: {...}  STAYS: {...}  PERSONA/WEIGHTS: {...}
Return JSON {leg_choices:[{leg_id, option_index, why}], stay_choices:[...],
headline, total_reasoning}.
```

### 13.5 Explanation (`muscle`, temp 0.4) — for non-hero items / copilot

```
In ≤ 2 sentences, tell a {persona} traveller why this option fits, referencing 2
concrete facts (price/distance/rating/baggage/timing). Then one honest tradeoff.
Never mention internal scores or rankings. If asked who should avoid it, say plainly.
ITEM: {compact}  CHEAPEST_PEER: {compact}  CONTEXT: {persona, vibes}
Return JSON {why, tradeoff, best_for, not_for}.
```

### 13.6 Learning / distillation (`muscle`) — **exists**, keep

`user_memory.distill_from_trip` — extend schema to also emit `axes` (importance 0..1
per axis) so learned weights persist to `traveller_profiles.axes`.

### 13.7 Re-rank consistency guard

The explanation prompt and the ranking share the **same context object**; the
explainer is told "this WAS chosen — justify it," never "choose." This is the
structural fix for the "why did you choose X" contradiction: explanation can't
disagree with a pick it's handed.

---

## Part 14 — Engineering (pipelines, caching, workers)

### 14.1 Module layout (new files)

```
app/reco/
  __init__.py
  intent.py          # Part 2 — extract_intent(), cached
  profile.py         # Part 2 — build_ranking_context(), merges memory + session
  features.py        # Part 7 — pure feature fns (moved/extended from scoring.py)
  rank.py            # Part 8 — weighted_present(), blend(), persona weights
  rerank.py          # Part 8 — wraps recommendation_agent (L3)
  journey.py         # Part 5 — journey_score(), wraps journey_reasoning
  personality.py     # Parts 3/4 — rule_tags() + LLM tag pass
  embeddings.py      # Part 6 — embed_texts(), doc builders
  vector_store.py    # pgvector queries (ANN, nearest)
  cache.py           # Redis helpers
  feedback.py        # Part 9 — log events, session bias
app/services/embeddings.py   # thin Vertex embedding client (batched, retried)
app/workers/reco_tasks.py    # Celery: embed_hotel, profile_hotel, nightly jobs
```

`app/services/scoring.py` stays as the deterministic core; `app/reco/rank.py` calls
into it and adds the new features. No big-bang rewrite — the current
`score_hotels/score_flights` keep working through migration.

### 14.2 Online pipeline (per request)

```
scrapers (existing, parallel) ─┐
master-table enrich ───────────┼─► union+dedup ─► hard_filter ─► features ─► rank(L2)
vector ANN recall ─────────────┘                                              │
                                                                              ▼
                                              SSE: emit L2 list ──► L3 rerank hero ──► SSE patch
                                                                              │
                                                              async ─► persist transport_options,
                                                                        rec_feedback impressions
```

### 14.3 Offline / background pipeline

- **On hotel upsert** (hook into `_hotel_master_rows` write): enqueue
  `embed_hotel(hotel_id)` + `profile_hotel(hotel_id)` (Celery). Idempotent via
  `doc_hash` — re-embed only if the doc changed.
- **On transport search:** async `insert transport_options` (batch, best-effort, same
  `run_db_resilient` pattern as hotels).
- **Nightly (Celery beat):** `expected_price` recompute, `destination_priors`
  refresh, user embedding refresh, `ranking_weights` bandit update, sitemap gen.

### 14.4 Latency optimization checklist

1. Never embed / never LLM on the blocking path (both precomputed or streamed).
2. Vectorize L2 with numpy over the whole candidate array (not a Python loop per
   item) — one pass, ~1 ms/100 items.
3. Cache intent + profile embedding + explanations aggressively (Part 12.7).
4. L3 sees ≤ 8 items, `max_output_tokens ≤ 512`, streamed.
5. `asyncio.gather` scrapers with a hard `wait_for` budget (already the pattern in
   `booking/flights.py` — 25 s cap). Partial results rank fine.
6. HNSW `ef_search` tuned to 40 (recall/latency knee for top-20).

### 14.5 Failure modes (graceful degradation)

| Down | Behaviour |
|---|---|
| Vertex (LLM) | L2 order ships; deterministic `why_tags` explanations. No user-visible failure. |
| Embeddings/pgvector | `SemanticMatch` treated as absent → renormalized out (§7). Pure feature ranking. |
| Redis | Recompute intent/profile inline (slower, still correct). |
| A scraper | Partial candidate set; existing per-provider try/except already isolates. |
| `transport_options` write | Logged, dropped — never affects the response (training data is best-effort). |

---

## Part 15 — Explainability

Contract: **every shipped item carries a human `why`; no internal number is ever
exposed.** `rank_score`/`final`/cosine stay server-side; the client sees:

- `why` — 1–2 sentences, concrete numbers (from L3 for hero, deterministic
  `why_tags` for the rest).
- `why_tags` — chips (`scoring.py` already produces these).
- `tradeoff` — the honest downside ("₹900 more; 20 min further from the beach").
- `best_for` / `not_for` — who should book / avoid (from `hotel_profiles`).
- `match` — bucketed label (`high`/`good`/`fair`), **never** a raw score.
- `alternatives` — 2–3 labelled options ("cheapest that's still central", "the
  splurge"), so the user sees the frontier, not one opaque answer.

Scores map to buckets: `final ≥ 78 → high`, `≥ 62 → good`, else `fair`. Buckets, not
numbers, because a "73.4" invites "why not 80" and exposes the model.

---

## Part 16 — Future AI (2–5 years)

1. **Regret-minimizing recommendations** — optimize for *"you won't wish you'd booked
   the other one"* using post-trip review deltas, not click-through. Booking optimizes
   conversion; we optimize satisfaction.
2. **Journey simulation** — simulate the actual day: "land 14:20, 40-min transfer,
   check-in 15:30, sunset at the rooftop by 18:00." Sell the *experience timeline*,
   not a list. No OTA does this.
3. **Weather/crowd-adaptive re-ranking** — shift stays inland when a cyclone is
   forecast; surface shoulder-season value; de-rank an area during a festival crush
   (or up-rank it for someone who wants the festival).
4. **Group consensus recommender** — one journey that maximizes *joint* utility across
   a group's differing profiles (min-regret across members), for the group-trip
   feature that already exists (`trip_groups`).
5. **Budget-morphing** — "for ₹4k more the whole trip becomes X" — recommend at the
   *trip* budget frontier, moving money between transport and stay to maximize
   experience, not each in isolation.
6. **Proactive re-book alerts** — post-booking price/quality monitoring on
   `transport_options` history: "the fare you saw dropped ₹1,200 — rebook?"
7. **Taste transfer** — "you loved that Rishikesh riverside stay → here's its Udaipur
   equivalent," via cross-destination embedding nearest-neighbours.

---

## Part 17 — Implementation Roadmap

Milestones are shippable increments; each ends with something in prod. Assumes the
enriched parsers + `scoring.py` + `recommendation_agent.py` already in place (done).

### Week 1 — Feature backbone + absent-field-aware scoring
- `app/reco/features.py`: port + extend `scoring.py` features; add `weighted_present`
  (renormalize over present features) and cross-provider dedup (§8.2).
- Contextual persona weight dicts (Part 8.4) in a module (table later).
- **Fixes today's real bugs:** EMT thin-signal hotels no longer buried; Udaipur
  cross-provider merge. Tests: extend `tests/test_rank_hotels.py`.
- Ship behind a flag on `/search/*/smart`. **No new infra.**

### Week 2 — Traveller Understanding + persona weighting live
- `app/reco/intent.py` (`extract_intent`, cached) + `profile.py`
  (`build_ranking_context`). Wire into `recommendation_agent` context.
- Extend `user_travel_memory` → `traveller_profiles` (axes column). Distill writes axes.
- Persona-driven weights active. Copilot "why" uses shared context (fixes contradiction).

### Week 3 — Transport persistence + journey scoring
- `transport_options` table + async write hook (mirrors `_hotel_master_rows`). **The
  training-data flywheel starts here.**
- `app/reco/journey.py`: `journey_score` extending `journey_reasoning`; hidden
  hotel-night as a scored penalty; return-mirror coherence.
- `POST /recommend/journey` (SSE, L2-first then L3 hero).

### Week 4 — Vector layer (Phase 1 embeddings, 384-dim)
- `pgvector` extension; `hotel_embeddings` (vector 384) + HNSW; `app/services/embeddings.py`.
- Celery `embed_hotel` on upsert, `doc_hash`-gated (§6.3); backfill the ~3,987 master hotels.
- Semantic **re-rank over top-50** live (skipped entirely when embeddings absent → zero-risk).
- Traveller profile embedding + cosine personalization.

### Week 5 — Hotel Intelligence + SEO surface
- `hotel_profiles` + `personality_centroids` tables; `personality.py` (rules →
  cluster → `muscle` only on low confidence); Celery `profile_hotel`, `doc_hash`-gated.
  Backfill top destinations first.
- `slug` + public `/hotels/<slug>` data contract; nightly sitemap; internal-link
  graph from nearest-neighbour. **Satisfies the SEO ask.**

### Week 6 — Feedback loop + online learning
- `rec_feedback` table + `POST /recommend/feedback`; impression logging in all
  `/recommend/*`.
- Redis session bias (§9.2) → live session re-ranking.
- `POST /recommend/explain`, `/recommend/hotels`, `/recommend/transport` GA.

### Week 7 — Offline learning + cold start
- Nightly Celery: `expected_price` (PriceFairness), `destination_priors` (cold
  start), user-embedding refresh.
- MMR diversity on first screen; cold-start priors wired.

### Week 8 — Experimentation + hardening
- `ranking_weights` table + hot-load; Thompson-sampling bandit over weight variants;
  offline replay eval on `rec_feedback` (NDCG/CVR).
- Load test the vector path; tune HNSW `ef_search`; dashboards
  (`app/core/metrics.py`): P95 per stage, LLM fallback rate, cache hit rate,
  coverage (% items with profile/embedding).

### Cross-cutting each week
- **Testing:** every feature fn unit-tested (pure → trivial); golden-set ranking
  tests (`tests/test_rank_*`); LLM outputs schema-validated + snapshot-tested.
- **Deployment:** flag-gated, L2-first so LLM/vector outages degrade silently.
- **Monitoring:** per-stage latency, fallback counters, feature-coverage gauges,
  offline NDCG vs the deterministic baseline.

---

## Appendix A — End-to-end pseudocode (hotels)

```python
async def recommend_hotels_endpoint(req, user):
    intent   = await extract_intent(req.text, req.intake, user.id)        # cached
    ctx      = await build_ranking_context(user.id, intent, session_of(req))
    raw      = req.candidates or await scrape_all_hotels(req)             # live, parallel
    cands    = dedup_cross_provider(raw)                                  # §8.2
    kept, relax = hard_filter(cands, intent.hard_constraints)            # §4.3 tiered
    embs     = await vector_store.get_hotel_embeddings([h.id for h in kept])
    arr      = to_feature_matrix(kept, ctx, embs)                        # numpy, §7
    scores   = weighted_present(arr, persona_weights(ctx.persona))       # §7/§8, vectorized
    scores   = blend(scores, cosine(embs, ctx.profile_embedding))        # §6
    order    = argsort(-scores)
    ranked   = [attach_why(kept[i], scores[i], kept) for i in order]     # why_tags + match bucket
    hero     = await llm_rerank_hotels(ranked[:8], ctx)                  # L3, streamed; may be None
    fire_and_forget(log_impressions(ranked, user, ctx))                  # §9
    return assemble(hero or ranked[0], ranked, relax, intent)
```

## Appendix B — What to build first if you build one thing

**Week 1 + Week 3.** Absent-field-aware scoring with cross-provider merge fixes the
*live* recommendation-quality bug (the ₹4,435-over-₹849 mistake, EMT thin hotels),
and `transport_options` starts the data flywheel that everything downstream (price
fairness, learned ranker, demand curves) depends on — with zero new infra and no LLM
risk. Vectors and SEO (Weeks 4–5) compound value but aren't the critical path to a
noticeably smarter recommender tomorrow.
```
