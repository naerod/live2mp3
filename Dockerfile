# syntax=docker/dockerfile:1

# ---- Stage 1 : compile bbc/audiowaveform ----------------------------------
# Images épinglées par tag daté + digest : un rebuild produit la même base.
FROM debian:bookworm-20251020-slim@sha256:78d2f66e0fec9e5a39fb2c72ea5e052b548df75602b5215ed01a17171529f706 AS audiowaveform
RUN apt-get update && apt-get install -y --no-install-recommends \
        git cmake make g++ libmad0-dev libid3tag0-dev libsndfile1-dev \
        libgd-dev libboost-filesystem-dev libboost-program-options-dev \
        libboost-regex-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Version épinglée (tag 1.10.3, commit vérifié) au lieu de HEAD.
ARG AUDIOWAVEFORM_TAG=1.10.3
ARG AUDIOWAVEFORM_COMMIT=cf17655089fb31b2b642b77d8c13c9960674eb4a
RUN git clone --depth 1 --branch "${AUDIOWAVEFORM_TAG}" https://github.com/bbc/audiowaveform.git /src \
    && test "$(git -C /src rev-parse HEAD)" = "${AUDIOWAVEFORM_COMMIT}" \
    && cd /src && mkdir build && cd build \
    && cmake -D ENABLE_TESTS=0 .. && make -j"$(nproc)" \
    && cp audiowaveform /usr/local/bin/

# ---- Stage 2 : runtime ----------------------------------------------------
FROM python:3.11.18-slim-bookworm@sha256:94a8fee35252d06ab75856f15f12c7bae67b10e8a6da93c7fded82b2610926cd AS runtime
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg genisoimage chromium \
        libmad0 libid3tag0 libsndfile1 libgd3 \
        libboost-filesystem1.74.0 libboost-program-options1.74.0 \
        libboost-regex1.74.0 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=audiowaveform /usr/local/bin/audiowaveform /usr/local/bin/audiowaveform

# Runtime JavaScript (deno) requis par yt-dlp pour résoudre le challenge nsig
# de YouTube. Sans lui, certains formats renvoient HTTP 403 Forbidden
# (incident 2026-08-13). yt-dlp détecte deno automatiquement sur le PATH.
COPY --from=denoland/deno:bin-2.9.5 /deno /usr/local/bin/deno

# Dépendances Python épinglées (versions testées, cf. requirements.txt).
# yt-dlp reste volontairement NON épinglé : YouTube casse régulièrement les
# anciennes versions (nsig, 403) ; un rebuild doit récupérer le correctif.
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt yt-dlp

COPY backend/ backend/
COPY frontend/ frontend/
COPY templates/ templates/
COPY VERSION .

ENV CHROMIUM_BIN=/usr/bin/chromium
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
