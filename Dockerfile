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
RUN mkdir -p /tmp/travbuddy/places_cache

EXPOSE 8000

# --workers 4: multiple processes so one slow request can't stall the API.
# --proxy-headers + --forwarded-allow-ips: trust X-Forwarded-For from Traefik,
#   otherwise every client shares Traefik's IP and per-IP rate limits break.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--proxy-headers", "--forwarded-allow-ips", "*"]
