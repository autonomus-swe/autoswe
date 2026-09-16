FROM python:3.12-slim
# ripgrep is search_code's fast path; it falls back to pure Python without it
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# gitleaks reads commits, so it runs here rather than in the sandbox: a worktree's git
# objects live in the bare clone on the worker, and the container never sees them.
# Pinned to the version .pre-commit-config.yaml uses, so local, CI and the worker agree
# about what counts as a secret — three different answers to that would be worse than one
# strict one.
ARG GITLEAKS_VERSION=8.24.3
RUN curl -sSfL -o /tmp/gitleaks.tar.gz \
      "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
    && tar -xzf /tmp/gitleaks.tar.gz -C /usr/local/bin gitleaks \
    && rm /tmp/gitleaks.tar.gz \
    && gitleaks version
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --all-extras --no-install-project
COPY . .
RUN uv sync --frozen --all-extras
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
