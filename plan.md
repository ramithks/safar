# TravBuddy Backend — Implementation Plan

## What We're Building
A backend API that takes an Instagram Reel URL and returns a structured location timeline:
every location shown in the video, enriched with place details, coordinates, ratings, and timestamps.

---

## Pipeline (end to end)

```
Instagram URL
    │
    ▼
[1] Video Download          yt-dlp → instaloader → gallery-dl (fallback chain)
    │
    ▼
[2] Scene Detection         PySceneDetect ContentDetector (hard cuts)
                            → AdaptiveDetector fallback (crossfades)
                            → Time-based fallback (1 frame/10s if no cuts)
    │
    ▼
[3] Frame Extraction        OpenCV — mid-frame per scene, resize to 1024px max
    │
    ▼
[4] Deduplication           pHash (imagehash) — Hamming distance < 10 → skip
                            Cuts Vision API calls by ~35%
    │
    ▼
[5] Vision AI               Google Vision API (Landmark + Web Detection, BATCH)
                            → Priority 1: Landmark Detection (returns lat/lng directly)
                            → Priority 2: Web Detection entities
                            → Gemini 2.5 Flash REST API (returns name, address, city, country, lat, lng directly)
    │
    ▼
[6] Places Enrichment       Google Places API (New)
                            → Nearby Search if lat/lng known
                            → Text Search if name only
                            → Place Details (Essentials fields only — saves 70% cost)
                            → diskcache by place_id (7-day TTL)
    │
    ▼
[7] Aggregate & Return      Deduplicate by place_id, order by first appearance
```

---

## Downloader Fallback Chain

Three downloaders tried in sequence, **no auth or cookies required**. If one fails, next is attempted automatically.

| Priority | Library | Notes |
|---|---|---|
| 1 | `yt-dlp` | Most reliable, actively maintained, public reels work without auth |
| 2 | `gallery-dl` | Different HTTP client + extraction path — independent failure from yt-dlp |
| 3 | `instaloader` | Public-only mode, no session, hits different Instagram endpoints |

---

## API Design

### Endpoints

```
POST   /api/v1/reels/analyze              Submit a job (async, returns job_id)
GET    /api/v1/reels/jobs/{job_id}        Poll status + progress
GET    /api/v1/reels/jobs/{job_id}/result Full result JSON
GET    /api/v1/reels/jobs/{job_id}/stream SSE stream (live location_found events)
GET    /api/v1/health                     Health check + dependency status
```

### Request
```json
{
  "reel_url": "https://www.instagram.com/reels/ABC123/",
  "options": {
    "min_confidence": 0.60,
    "max_locations": 20,
    "enrich_places": true,
    "webhook_url": "https://yourapp.com/webhook",
    "webhook_secret": "whsec_..."
  },
  "metadata": { "user_id": "usr_123" }
}
```

### Response (result)
```json
{
  "job_id": "job_01HX...",
  "status": "completed",
  "reel": { "url": "...", "duration_seconds": 58, "frame_count": 9 },
  "summary": {
    "total_locations": 4,
    "dominant_city": "Tokyo",
    "dominant_country": "Japan",
    "geographic_bbox": { "north": 35.69, "south": 35.65, "east": 139.77, "west": 139.69 }
  },
  "timeline": [
    {
      "segment_id": "seg_001",
      "timestamp_start_seconds": 0.0,
      "timestamp_end_seconds": 14.2,
      "confidence": 0.94,
      "detection_source": "landmark_recognition",
      "place": {
        "place_id": "ChIJ...",
        "name": "Senso-ji Temple",
        "formatted_address": "...",
        "coordinates": { "lat": 35.7148, "lng": 139.7967 },
        "rating": 4.6,
        "types": ["tourist_attraction"],
        "google_maps_url": "https://maps.google.com/?cid=..."
      }
    }
  ],
  "processing_stats": { "total_duration_ms": 27340, "frames_analyzed": 9 }
}
```

### Error Envelope (all errors)
```json
{
  "error": {
    "code": "REEL_ACCESS_DENIED",
    "message": "This Reel belongs to a private account.",
    "retryable": false,
    "retry_after_seconds": null
  },
  "job_id": "job_01HX...",
  "status": "failed"
}
```

**Error codes:** `REEL_NOT_FOUND`, `REEL_ACCESS_DENIED`, `DOWNLOAD_FAILED`, `DOWNLOAD_TIMEOUT`,
`INVALID_URL`, `VIDEO_TOO_LONG`, `NO_LOCATIONS_DETECTED`, `VISION_API_ERROR`,
`PLACES_API_ERROR`, `QUOTA_EXCEEDED`, `RATE_LIMITED`

---

## Cost Per Video (approximate)

| Component | Cost |
|---|---|
| yt-dlp download | $0.00 |
| Scene detection + frame extraction | $0.00 |
| Google Vision (5 frames, Landmark + Web) | ~$0.008 |
| Google Places Text Search (2 lookups) | ~$0.034 |
| Google Places Details — Essentials (4 places) | ~$0.020 |
| Compute (Cloud Run, ~30s) | ~$0.001 |
| **Total** | **~$0.07–$0.10** |

Cost drops to ~$0.05 after 1 month due to Places cache hit rate > 60%.
First 1,000 Vision API calls/month are free (Google free tier).

---

## Project Structure

```
travbuddy_backend/
├── app/
│   ├── main.py                    # FastAPI app, lifespan, routers
│   ├── config.py                  # pydantic-settings, all env vars
│   ├── models/
│   │   ├── request.py             # AnalyzeReelRequest
│   │   └── response.py            # JobResponse, LocationResult, PlaceDetail
│   ├── api/
│   │   └── v1/
│   │       ├── router.py
│   │       ├── analyze.py         # POST /reels/analyze
│   │       ├── jobs.py            # GET /jobs/{id}, /result, /stream
│   │       └── health.py
│   ├── pipeline/
│   │   ├── orchestrator.py        # Runs all steps, cleanup
│   │   ├── downloader.py          # yt-dlp + instaloader + gallery-dl chain
│   │   ├── scene_detector.py      # PySceneDetect wrapper
│   │   ├── frame_extractor.py     # OpenCV extraction + resize
│   │   ├── deduplicator.py        # pHash deduplication
│   │   ├── vision_client.py       # Google Vision API (batch)
│   │   └── places_client.py       # Google Places API + diskcache
│   ├── workers/
│   │   └── celery_app.py          # Celery + Redis config
│   └── utils/
│       ├── temp_files.py          # Context manager for /tmp cleanup
│       └── cost_tracker.py        # Tracks API calls per job
├── tests/
│   ├── test_downloader.py
│   ├── test_scene_detector.py
│   ├── test_vision_client.py
│   └── test_pipeline.py
├── .env.example
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── plan.md                        # This file
```

---

## Implementation Order

1. `requirements.txt` + `.env.example` + `config.py`
2. `models/` — Pydantic schemas
3. `pipeline/downloader.py` — fallback chain (yt-dlp → instaloader → gallery-dl)
4. `pipeline/scene_detector.py`
5. `pipeline/frame_extractor.py`
6. `pipeline/deduplicator.py`
7. `pipeline/vision_client.py`
8. `pipeline/places_client.py`
9. `pipeline/orchestrator.py`
10. `workers/celery_app.py`
11. `api/v1/` — all endpoints
12. `app/main.py`
13. `Dockerfile` + `docker-compose.yml`
14. `tests/`

---

## Environment Variables

```
# Google APIs
GOOGLE_API_KEY=AIza...
GOOGLE_MAPS_API_KEY=AIza...

# Pipeline tuning
SCENE_DETECTION_THRESHOLD=27.0
SCENE_MIN_LENGTH_FRAMES=15
MAX_UNIQUE_FRAMES=10
MIN_VISION_CONFIDENCE=0.60
FRAME_MAX_DIMENSION=1024
FRAME_JPEG_QUALITY=85

# Limits
MAX_VIDEO_DURATION_SECONDS=300
MAX_VIDEO_SIZE_MB=200
MAX_LOCATIONS=20

# Cache
PLACES_CACHE_DIR=/tmp/travbuddy/places_cache
PLACES_CACHE_TTL_SECONDS=604800

# Job Queue
REDIS_URL=redis://localhost:6379/0

# Instagram Auth (optional but recommended)
INSTAGRAM_COOKIES_FILE=/etc/travbuddy/instagram_cookies.txt
INSTALOADER_SESSION_FILE=/etc/travbuddy/instaloader.session

# App
APP_ENV=development
LOG_LEVEL=INFO
TEMP_DIR=/tmp/travbuddy
```

---

## Key Design Decisions

- **Async jobs only** — Processing takes 20-45s. Sync would timeout.
- **Google Vision over LLMs** — Purpose-built, returns lat/lng directly, cheaper.
- **Essentials Places fields only** — $5/1k vs $17/1k. Add Pro fields only when needed.
- **pHash dedup before Vision** — Eliminates ~35% of API calls before they happen.
- **diskcache for Places** — Same landmark queried across 100 reels = 99 free lookups.
- **3-downloader fallback** — Instagram breaks scrapers regularly. Redundancy is essential.
- **SSE progress stream** — `location_found` events as they arrive = reel builds live in UI.
