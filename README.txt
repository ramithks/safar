================================================================
  TRAVBUDDY BACKEND
  Instagram Reel → Location Timeline Pipeline
================================================================

STACK
-----
  FastAPI + Celery + Redis
  yt-dlp (video download)
  PySceneDetect + OpenCV (scene detection + frame extraction)
  Gemini 2.5 Flash (location identification via vision AI)
  pHash deduplication (imagehash)


----------------------------------------------------------------
  PREREQUISITES
----------------------------------------------------------------

  1. Python 3.11+
  2. Redis running on localhost:6379
  3. yt-dlp installed globally:  pip install yt-dlp
  4. ffmpeg installed:            brew install ffmpeg

  Start Redis (if not already running):
    brew services start redis
    -- or --
    redis-server


----------------------------------------------------------------
  SETUP
----------------------------------------------------------------

  cd travbuddy_backend

  # Create and activate virtual environment
  python -m venv venv
  source venv/bin/activate

  # Install dependencies
  pip install -r requirements.txt

  # Create .env file
  cp .env.example .env
  # Then edit .env and set GEMINI_API_KEY


----------------------------------------------------------------
  .env FILE
----------------------------------------------------------------

  GEMINI_API_KEY=your_gemini_api_key_here
  REDIS_URL=redis://localhost:6379/0
  LOG_LEVEL=INFO

  # Optional — only needed if you want Places API enrichment
  # GOOGLE_MAPS_API_KEY=your_maps_key


----------------------------------------------------------------
  RUNNING THE APP
----------------------------------------------------------------

  You need TWO terminals:

  Terminal 1 — Start FastAPI server:
    source venv/bin/activate
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

  Terminal 2 — Start Celery worker:
    source venv/bin/activate
    celery -A app.workers.celery_app.celery_app worker --loglevel=info --concurrency=2

  API is now live at:  http://localhost:8000
  API docs (Swagger):  http://localhost:8000/docs


----------------------------------------------------------------
  ACCESS FROM PHYSICAL DEVICE (same WiFi)
----------------------------------------------------------------

  Find your Mac's local IP:
    ipconfig getifaddr en0

  Then use that IP instead of localhost:
    http://192.168.x.x:8000


----------------------------------------------------------------
  CURL EXAMPLES
----------------------------------------------------------------

  1. SUBMIT A REEL FOR PROCESSING
  --------------------------------
  curl -X POST http://localhost:8000/api/v1/reels/analyze \
    -H "Content-Type: application/json" \
    -d '{
      "reel_url": "https://www.instagram.com/reel/ABC123/"
    }'

  Response:
  {
    "job_id": "job_01HXZ...",
    "status": "queued",
    "poll_url": "http://localhost:8000/api/v1/reels/jobs/job_01HXZ...",
    "stream_url": "http://localhost:8000/api/v1/reels/jobs/job_01HXZ.../stream"
  }


  2. CHECK JOB STATUS (POLLING)
  ------------------------------
  curl http://localhost:8000/api/v1/reels/jobs/JOB_ID

  Response includes:
  - status: queued | processing | completed | failed
  - progress.percent_complete: 0-100
  - progress.stage: current pipeline stage


  3. GET FINAL RESULT (once status = "completed")
  ------------------------------------------------
  curl http://localhost:8000/api/v1/reels/jobs/JOB_ID/result

  Returns full timeline with:
  - place.name — location name from Gemini AI
  - place.google_maps_url — Google Maps search link
  - place.address — city, country
  - place.coordinates — lat/lng
  - frame_thumbnail_b64 — extracted video frame as base64 JPEG
  - timestamp_start_seconds / timestamp_end_seconds


  4. STREAM LIVE PROGRESS (Server-Sent Events)
  ---------------------------------------------
  curl -N http://localhost:8000/api/v1/reels/jobs/JOB_ID/stream

  Emits events as they happen:
    event: stage_start   → pipeline stage began
    event: stage_complete → stage finished with duration
    event: location_found → a place was identified (live!)
    event: completed      → job done, total_locations count
    event: failed         → job failed with error


  5. HEALTH CHECK
  ---------------
  curl http://localhost:8000/health


  6. FULL EXAMPLE (submit → wait → get result)
  ---------------------------------------------
  # Step 1: Submit
  JOB=$(curl -s -X POST http://localhost:8000/api/v1/reels/analyze \
    -H "Content-Type: application/json" \
    -d '{"reel_url": "https://www.instagram.com/reel/DNqRBwnTxae/"}' \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

  echo "Job ID: $JOB"

  # Step 2: Poll until done
  watch -n 2 "curl -s http://localhost:8000/api/v1/reels/jobs/$JOB | python3 -m json.tool"

  # Step 3: Get result
  curl -s http://localhost:8000/api/v1/reels/jobs/$JOB/result | python3 -m json.tool


----------------------------------------------------------------
  PIPELINE STAGES
----------------------------------------------------------------

  1. Download       — yt-dlp (gallery-dl / instaloader fallback)
  2. Scene Detection — PySceneDetect (ContentDetector)
  3. Frame Extraction — OpenCV mid-scene frames + pHash dedup
  4. Vision Analysis — Gemini 2.5 Flash identifies each location
  5. Timeline Build  — merge duplicate locations by name

  Typical processing time: 20-45 seconds per reel
  Cost: ~$0.001 per reel (Gemini tokens only)


----------------------------------------------------------------
  DEDUPLICATION BEHAVIOUR
----------------------------------------------------------------

  If a reel is filmed entirely at one café (e.g. 10 scene cuts,
  same location), all frames resolve to the same place name and
  get merged into ONE timeline segment spanning the full video.
  The highest-confidence frame is kept as the thumbnail.


----------------------------------------------------------------
  TROUBLESHOOTING
----------------------------------------------------------------

  "Requested format is not available" (yt-dlp)
    → Fixed in code. Uses wide format fallback chain.

  "403 / login redirect" (gallery-dl / instaloader)
    → Expected for authenticated-only reels. yt-dlp handles most
      public reels without cookies.

  "Expecting value" on Gemini response
    → Gemini 2.5 Flash is a thinking model — response has thought
      tokens in parts[0]. Code reads last non-thought part.

  "Places API REQUEST_DENIED"
    → Places API is disabled by default (cost = $0). Pipeline
      runs fine with Gemini-only data.

  Worker not picking up jobs
    → Make sure Redis is running: redis-cli ping  →  PONG
    → Make sure worker and server use same .env / REDIS_URL


================================================================
