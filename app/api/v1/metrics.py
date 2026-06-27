"""
GET  /api/v1/metrics/scrapers — live scraper observability snapshot.
POST /api/v1/metrics/scrapers/reset — clear counters (e.g. before a load test).

Per-provider latency (avg/p50/p95), failure %, proxy %, captcha %, timeout %,
blocked %, avg result count, and duplicate %. Left unauthenticated like /health
so an ops dashboard or uptime probe can scrape it; it exposes no user data.
"""
from fastapi import APIRouter

from app.core.metrics import METRICS

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/scrapers")
async def scraper_metrics():
    return METRICS.snapshot()


@router.post("/scrapers/reset")
async def reset_scraper_metrics():
    METRICS.reset()
    return {"status": "reset"}
