FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --all-extras --no-install-project
COPY . .
RUN uv sync --frozen --all-extras
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
