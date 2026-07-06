# ============================================================================
# Akash AI Pro Gateway -- Backend (FastAPI) Dockerfile
# Multi-stage build: keeps the final runtime image slim and free of
# build-only tooling (compilers, headers), reducing attack surface.
# ============================================================================

# ---- Build stage ------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ---- Runtime stage -----------------------------------------------------------
FROM python:3.11-slim

# Run as a non-root user -- defense in depth.
RUN groupadd -r akash && useradd -r -g akash akash

WORKDIR /app

COPY --from=builder /root/.local /home/akash/.local
COPY src ./src

RUN mkdir -p /app/keys /app/logs && chown -R akash:akash /app

ENV PATH=/home/akash/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER akash

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')" || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
