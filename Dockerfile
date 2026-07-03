# syntax=docker/dockerfile:1

# ---- Stage 1 : compile bbc/audiowaveform ----------------------------------
FROM debian:bookworm-slim AS audiowaveform
RUN apt-get update && apt-get install -y --no-install-recommends \
        git cmake make g++ libmad0-dev libid3tag0-dev libsndfile1-dev \
        libgd-dev libboost-filesystem-dev libboost-program-options-dev \
        libboost-regex-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/bbc/audiowaveform.git /src \
    && cd /src && mkdir build && cd build \
    && cmake -D ENABLE_TESTS=0 .. && make -j"$(nproc)" \
    && cp audiowaveform /usr/local/bin/

# ---- Stage 2 : runtime ----------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg genisoimage chromium \
        libmad0 libid3tag0 libsndfile1 libgd3 \
        libboost-filesystem1.74.0 libboost-program-options1.74.0 \
        libboost-regex1.74.0 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=audiowaveform /usr/local/bin/audiowaveform /usr/local/bin/audiowaveform

# yt-dlp (dernière version via pip)
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt yt-dlp

COPY backend/ backend/
COPY frontend/ frontend/
COPY templates/ templates/

ENV CHROMIUM_BIN=/usr/bin/chromium
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
