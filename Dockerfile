FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies only (skip local package for caching)
RUN uv sync --frozen --no-dev --extra cloud --extra gdrive --no-install-project

# Copy application code
COPY src/ src/
COPY migrations/ migrations/

# Install the local package itself
RUN uv sync --frozen --no-dev --extra cloud --extra gdrive

# Railway injects PORT; defaults handled in config.py
ENV MCP_TRANSPORT=streamable-http

CMD ["uv", "run", "erika-mcp"]
