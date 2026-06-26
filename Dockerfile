# Dockerfile — DUWASA DMA Hydraulic Simulation Service
# Multi-stage build targeting Python 3.12 (matching the bundled EPyT-Flow venv).

# ── stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# EPyT-Flow needs a compiler stack; GIS packages are installed from wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ gfortran libgfortran5 make \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /venv \
    && /venv/bin/pip install --upgrade pip \
    && /venv/bin/pip install --no-cache-dir -r requirements.txt

# ── stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL maintainer="DUWAS Engineering"
LABEL description="DMA Hydraulic Simulation Service (EPyT-Flow/EPANET)"

# libgomp is needed by the EPANET C library at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the built venv and application code
COPY --from=builder /venv /venv
COPY app/ ./app/
COPY frontend/ ./frontend/

# Shared data directories (mounted from the host via volumes)
RUN mkdir -p /data/gpkg /data/db

ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://localhost:{os.getenv(\"PORT\", \"8080\")}/health')"

# Single worker — EPyT-Flow is not thread-safe; scale with multiple containers.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --log-level info"]
