FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies (cloud extras for Turso)
RUN uv sync --frozen --no-dev --extra cloud

# Copy application code
COPY src/ src/
COPY migrations/ migrations/

# Railway injects PORT; defaults handled in config.py
ENV MCP_TRANSPORT=streamable-http

CMD ["uv", "run", "erika-mcp"]
