#!/usr/bin/env bash
# Safar start script — free single-service deploys.
# Web only by default; RUN_WORKER=true also runs the Celery worker + beat
# in-container (requires REDIS_URL, e.g. a free Upstash database).
set -e

PORT="${PORT:-8000}"

if [ "${RUN_WORKER:-false}" = "true" ]; then
  echo "[start] RUN_WORKER=true — starting Celery worker + beat in background"
  celery -A app.workers.celery_app:celery_app worker \
    --loglevel=INFO --concurrency="${CELERY_WORKER_CONCURRENCY:-2}" &
  celery -A app.workers.celery_app:celery_app beat --loglevel=INFO &
fi

echo "[start] web on :${PORT}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --workers 1 \
  --proxy-headers --forwarded-allow-ips '*'
