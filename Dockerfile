# Commission Statement Reader.
#
# A Dockerfile rather than Nixpacks auto-detection, for one reason: this app
# needs Tesseract, a system package pip cannot install. Auto-detection would
# build a working-looking image in which every scanned carrier silently stops
# being readable. Making the apt layer explicit keeps that visible.

FROM python:3.12-slim

# 3.12, not 3.14: the code needs 3.10+ (verified - no match statements, no PEP
# 695 generics), and slim images for 3.12 are long-settled. Pinned rather than
# floating so a rebuild months from now produces the same runtime.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# tesseract-ocr    the OCR engine (app/ocr.py finds it via PATH)
# tesseract-ocr-eng + osd
#                  language data and the orientation/script model. osd is NOT
#                  optional here: app/ocr.py calls image_to_osd() to decide page
#                  rotation, and several carriers ship sideways scans.
# libgl1, libglib2.0-0
#                  shared libraries Pillow/pypdfium2 need to rasterise pages.
RUN apt-get update && apt-get install --no-install-recommends -y \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-osd \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies before source, so editing code does not invalidate the pip layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Run as a non-root user. The data directory is created and handed over here so
# the volume mount point exists and is writable before the app starts.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data/uploads \
    && chown -R appuser:appuser /app
USER appuser

# Where the SQLite database and uploaded files live. This is the path a
# persistent volume must be mounted at; see docs/DEPLOYMENT.md. Without a volume
# here, every deploy starts with an empty history and no accounts.
ENV DATA_DIR=/app/data
VOLUME ["/app/data"]

# Documentation only - the platform decides the real port via $PORT.
EXPOSE 8000

# Bound to 0.0.0.0, unlike the Tailscale setup which binds 127.0.0.1 on purpose:
# a container's loopback is not reachable from outside it.
#
# A shell is needed so ${PORT} expands, but `exec` matters: without it uvicorn
# runs as a child of /bin/sh, the shell receives SIGTERM on shutdown and uvicorn
# never does, so the platform kills it after the grace period instead. This app
# writes SQLite, so an abrupt kill mid-write is worth avoiding. `exec` replaces
# the shell with uvicorn, which then gets signals directly as PID 1.
#
# One worker: OCR is CPU-bound and a second worker would double peak memory
# (measured 224 MB for one 7-page scan) for no throughput gain on one vCPU.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
