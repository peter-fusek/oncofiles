# Production image for oncofiles.
#
# Built by GitHub Actions (.github/workflows/ci.yml, job `build`) for
# linux/arm64 and pushed to ghcr.io/peter-fusek/oncofiles:<sha>. The Mac mini
# VM only pulls it (deploy/Dockerfile.coolify) — no compiling and no GitHub
# release downloads on the production host (Railpack's `mise install` fetched
# Python and uv anonymously from GitHub releases; a GitHub incident on
# 2026-11-08 made every deploy fail).
#
# Network needed at build time: Docker Hub (base image), Debian mirrors,
# PyPI (uv + locked dependencies), crates.io (libsql's Rust sources).
# Nothing is fetched at container start. No build-time secrets (#550).

ARG PYTHON_IMAGE=python:3.12-slim-trixie

# ── build stage: compile the locked environment ──────────────────────────
FROM ${PYTHON_IMAGE} AS build

# libsql 0.1.11 has no linux/aarch64 wheel on PyPI, so maturin compiles it.
# Its libsql-ffi build script drives CMake (SQLite3MultipleCiphers), so cmake
# is required next to the C toolchain; Debian's rustc 1.85 is new enough.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential cmake pkg-config cargo rustc \
    && rm -rf /var/lib/apt/lists/*

# uv from PyPI (pinned), not from GitHub releases.
RUN pip install --no-cache-dir uv==0.13.5

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12

WORKDIR /app

# Dependencies first: this slow layer (libsql compile) is reused until
# pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-install-project

# Editable install of the project itself: the code resolves paths from the
# source tree (config.BASE_DIR = /app → data/, migrations/, assets/landing/),
# so the venv points at /app/src via a .pth file.
COPY src ./src
RUN uv sync --locked

# ── runtime stage ─────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE}

# Links the GHCR package to the repository.
LABEL org.opencontainers.image.source="https://github.com/peter-fusek/oncofiles" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# curl: Coolify runs its health check (GET /health) inside the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY migrations ./migrations
COPY assets ./assets
# /app/data is the Coolify volume (SQLite DB); create it for runs without one.
RUN mkdir -p /app/data

EXPOSE 8080

# Installed entry point, not `uv run`: no re-sync, no network at start.
CMD ["oncofiles-mcp"]
