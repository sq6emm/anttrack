FROM debian:bookworm-slim

# python3-hamlib provides the Hamlib Python (SWIG) bindings ("import Hamlib")
# via apt, tied to Debian's system Python. The venv below is created with
# --system-site-packages so it can see that module while still letting pip
# install FastAPI/uvicorn/skyfield/pyhamtools without touching system
# packages (Debian's Python is "externally managed" as of bookworm).
# ffmpeg relays the optional RTSP camera feed into browser-playable MJPEG.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        python3-hamlib \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv --system-site-packages /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY anttrack ./anttrack
COPY track.py .
COPY config.ini.example .

# QTH/rotator config and satellite/ephemeris caches live here so they
# survive container restarts/rebuilds when this is mounted as a volume.
ENV ANTTRACK_DATA_DIR=/app/data
ENV ANTTRACK_CONFIG=/app/data/config.ini
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "anttrack.api:app", "--host", "0.0.0.0", "--port", "8000"]
