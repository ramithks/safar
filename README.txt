================================================================
  SAFAR BACKEND
================================================================

A FastAPI backend for AI-powered trip planning and Instagram
Reel → location timeline extraction.

STACK
-----
  FastAPI (async, Python 3.11+)
  Supabase (Postgres + Auth)
  Vertex AI / Gemini (route planning, itinerary generation)
  Google Places API (activity enrichment)
  RedBus, IRCTC, Booking.com, EaseMyTrip, Expedia (live transport/hotel)
  Rate limiting via SlowAPI


================================================================
  PROJECT STRUCTURE
================================================================

  app/
    api/v1/
      plan_trip/          ← Core trip planner (main feature)
        endpoint.py         POST /api/v1/plan
        plan.py             Orchestrates journey + itinerary in parallel
        journey.py          Multi-leg transport planner (bus/train/flight)
        orchestrator.py     Day-by-day itinerary generator (Gemini + Places)
        hydration.py        Google Places enrichment (3-tier, low API cost)
        confidence.py       Per-activity confidence scoring (anti-hallucination)
        validation.py       Route validation + trip health checks
        budget.py           Detailed budget breakdown (5 categories)
        helpers.py          Pure utility functions, currency config
        models.py           Pydantic request/response models
        stream.py           SSE streaming variant of the plan endpoint

      trips/              ← Saved trips CRUD
      groups/             ← Travel groups / communities
      discover/           ← Destination discovery
      search/             ← Search endpoint
      onboarding/         ← User onboarding
      users/              ← User profile
      reels/              ← Instagram Reel → location timeline

    services/
      transport.py          RedBus bus search
      flights.py            Flight search aggregator
      hotels.py             Hotel search aggregator
      railway.py            IRCTC train search
      places.py             Google Places API helpers
      vertex.py             Vertex AI / Gemini helpers
      booking/              Booking.com integration
      easemytrip/           EaseMyTrip integration
      expedia/              Expedia integration
      bus/                  RedBus + FlixBus

    pipeline/             ← Instagram Reel analysis pipeline
    core/                 ← Circuit breaker, rate limiter, SSE, timing
    db/                   ← Supabase client
    deps/                 ← FastAPI dependencies (auth)
    config.py             ← All settings (env vars)
    main.py               ← FastAPI app factory


================================================================
  SETUP
================================================================

  1. Create virtual environment
     python -m venv venv
     source venv/bin/activate

  2. Install dependencies
     pip install -r requirements.txt

  3. Set environment variables (create a .env file)
     SUPABASE_URL=...
     SUPABASE_SERVICE_ROLE_KEY=...
     GOOGLE_MAPS_API_KEY=...
     GOOGLE_CLOUD_PROJECT=...     (for Vertex AI / Gemini)
     REDBUS_API_KEY=...
     BOOKING_API_KEY=...
     EASEMYTRIP_API_KEY=...
     EXPEDIA_API_KEY=...
     SENTRY_DSN=...               (optional)

  4. Run the server
     uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

  API docs:  http://localhost:8000/docs


================================================================
  CORE ENDPOINT: POST /api/v1/plan
================================================================

  Plans a complete trip in a single call: transport (going + return)
  plus a day-by-day itinerary, all running in parallel.

  REQUEST BODY
  ------------
  {
    "origin":          "Delhi",
    "destination":     "Goa",
    "departure_date":  "2025-02-14",
    "days":            5,
    "travelers":       2,
    "budget":          "mid",       // "low" | "mid" | "high"
    "interests":       ["beach", "food"],
    "transport_modes": [],          // [] = Gemini picks best mode
    "return_date":     null,        // auto-computed if omitted
    "stay_nights":     null         // defaults to days - 1
  }

  RESPONSE SHAPE
  --------------
  {
    // Itinerary (from Gemini + Google Places)
    "trip_name":             "...",
    "destination":           "Goa",
    "destination_description": "...",
    "days": [
      {
        "day": 1,
        "theme": "...",
        "activities": [
          {
            "title":            "Baga Beach",
            "type":             "attraction",
            "confidence_score": 0.92,    // 0–1 anti-hallucination score
            "rating":           4.3,
            "user_ratings_total": 18000,
            "lat": 15.55, "lng": 73.75,
            "maps_url":         "...",
            "photo_url":        "...",
            "opening_hours":    [...],
            "time":             "09:00 AM",
            "duration":         "2h"
          }
        ]
      }
    ],
    "trip_quality": {
      "score":       85,            // 0–100 health score
      "issues":      [...],         // duplicates, overload detected
      "warnings":    [...],         // closed attractions, etc.
      "auto_fixed":  2              // activities removed by auto-fix
    },
    "trip_intelligence": {
      "pace":                   "Moderate",
      "budget_fit_score":       92,
      "travel_efficiency_score": 88,
      "avg_walking_distance_km": 1.4
    },

    // Transport — going journey
    "going": {
      "flights": { "items": [...], "recommended": {...} },
      "buses":   { "items": [...], "recommended": {...} },
      "trains":  { "items": [...], "recommended": {...} },
      "journey_options": {
        "cheapest": {
          "mode": "train", "cost_per_person": 850,
          "duration_display": "18h", "description": "Rajdhani Express"
        },
        "fastest": {
          "mode": "flight", "cost_per_person": 4500,
          "duration_display": "1h 30m", "description": "IndiGo 6E-123"
        },
        "balanced": {
          "mode": "flight", "cost_per_person": 3200,
          "duration_display": "2h", "description": "Air India AI-456"
        }
      },
      "legs":    [...],             // each leg has validation: {valid, confidence, issues}
      "hotels":  { "items": [...], "recommended": {...} }
    },

    // Transport — return journey
    "return": { ... },

    // Detailed budget (5 categories)
    "budget_breakdown": {
      "transport":  8000,
      "stay":       6000,
      "food":       6000,
      "activities": 4000,
      "misc":       1400,
      "total":      25400,
      "per_person": 12700,
      "hotel_per_night": 2000,
      "currency":   "₹"
    },

    "cost_breakdown":    { ... },   // live transport cost detail
    "origin":            "Delhi",
    "departure_date":    "2025-02-14",
    "return_date":       "2025-02-19",
    "travelers":         2,
    "budget":            "mid"
  }

  Rate limit: 10 requests / hour / IP


================================================================
  ANTI-HALLUCINATION FEATURES
================================================================

  1. CONFIDENCE SCORES (per activity)
     Each activity gets confidence_score: 0.0–1.0
     Signals: Google Places verified (+0.40), rating (+0.30),
              review count log-scale (+0.20), opening hours (+0.10)
     Activities below 0.30 are dropped (day keeps min 2).

  2. ROUTE VALIDATION (per transport leg)
     Each leg gets validation: {valid, confidence, issues, warnings}
     Checks: airport IATA resolved, station code resolved, bus stop
     found, live data returned, duration realistic for distance+mode.

  3. TRIP HEALTH CHECKS
     Detects: duplicate attractions (fuzzy match > 80%), activity
     overload (> 4/day), currently-closed places.
     Auto-fix: removes duplicates, caps to 4/day by confidence rank.

  4. GEMINI HUB ROUTING
     Prompt enforces intermediate hub routing for indirect routes
     (e.g. Bhiwani → Delhi → Haridwar, not a fake direct train leg).


================================================================
  INSTAGRAM REEL PIPELINE: POST /api/v1/reels/analyze
================================================================

  Extracts a location timeline from a public Instagram Reel.

  Stages:
    1. Download       — yt-dlp (gallery-dl / instaloader fallback)
    2. Scene Detection — PySceneDetect (ContentDetector)
    3. Frame Extraction — OpenCV + pHash deduplication
    4. Vision Analysis — Gemini vision (location identification)
    5. Timeline Build  — merge duplicate locations

  Cost: ~$0.001 per reel | Time: 20–45 seconds

  Endpoints:
    POST /api/v1/reels/analyze         → { job_id, poll_url, stream_url }
    GET  /api/v1/reels/jobs/{id}       → { status, progress }
    GET  /api/v1/reels/jobs/{id}/result → full location timeline
    GET  /api/v1/reels/jobs/{id}/stream → SSE live progress


================================================================
  RATE LIMITS
================================================================

  /api/v1/plan     — 10 requests / hour / IP
  All other routes — standard (no explicit limit unless noted)


================================================================
  HEALTH CHECK
================================================================

  GET /health  →  { status: "ok", version: "..." }


================================================================
