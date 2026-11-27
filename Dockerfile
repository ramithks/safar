FROM python:3.11-slim

# System deps for OpenCV, yt-dlp, ffmpeg (required for video processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Create temp dirs
RUN mkdir -p /tmp/safar/places_cache

EXPOSE 8000

# Free-tier friendly start: one web process (512 MB hosts), honouring the
# platform's $PORT. RUN_WORKER=true additionally starts the Celery worker and
# beat in the same container (needs REDIS_URL) for single-service free deploys.
COPY start.sh .
RUN chmod +x start.sh
CMD ["./start.sh"]
