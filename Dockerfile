# syntax=docker/dockerfile:1
#
# Production image for the harness HTTP API server.
#
# Runs `harness serve` against the bundled engram template, listening on
# $PORT (5000 by default). Better Base talks to this service over the
# Render private network; per-session memory keys, account ids, and
# workspace paths arrive in the create-session payload.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000

WORKDIR /app

# Git is required by the harness's engram git-backed memory ops.
# ca-certificates is required for outbound HTTPS to Anthropic / Better Base.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install the harness with the FastAPI server extras. Copy pyproject first
# so the layer caches independently of source changes.
COPY pyproject.toml ./
COPY harness/ ./harness/
COPY engram/ ./engram/

RUN pip install --no-cache-dir -e ".[api]"

# Bake the bundled engram template into the image. The lazy-bootstrap
# helper in `harness/server.py` copies from here into a brand-new
# account's memory dir on first dispatch.
ENV HARNESS_BUNDLED_MEMORY_DIR=/app/engram/core/memory

EXPOSE 5000

CMD ["sh", "-c", "harness serve --host 0.0.0.0 --port ${PORT:-5000}"]
