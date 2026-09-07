# Changelog

## 0.1.0 — Phase 1: single-agent loop

Added
- Docker sandbox: one container per run, non-root, read-only rootfs, tmpfs /tmp, only the
  worktree bind-mounted, all capabilities dropped, CPU/memory/pid limits. Created on the
  install network and disconnected before the agent starts.
- Repository layer: bare clone cache, per-run worktrees on `agent/<run-id>`, per-process
  git credentials, branch push and idempotent pull-request creation.
- Tool layer: bash, a file editor with view-staleness checks, `run_tests` with a
  pytest-json-report parser, and host-side git tools. A deny-list and path confinement
  back the sandbox boundaries; read-only roles are enforced by the registry at import.
- LLM gateway: provider protocol plus an OpenAI-compatible provider (OpenRouter by
  default; also vLLM, Ollama, Groq) with a hand-written tool loop and validated
  structured output. Per-role routing and pricing.
- Coder agent, orchestrator (SETUP, CODE, TEST, PR) with guaranteed teardown, and an arq
  worker.
- Control plane: `POST /runs`, `GET /runs/{id}`, `/healthz`, API-key auth with a per-key
  rate limit. CLI `autoswe run` and `autoswe status`.
- Migration 0002: monotonic `seq` on `tool_calls` and `llm_calls` so a run's actions
  replay in order.

## 0.0.1 — Phase 0: foundations

- Project scaffold: uv, ruff, mypy, pytest tiers, Makefile, pre-commit, CI.
- `core/settings.py`: pydantic-settings with fail-fast validation; worker-only secrets.
- `contracts/`: every structured-output schema from README §6 plus runtime wrappers.
- `storage/`: SQLAlchemy 2.0 models for the eight core tables, async Alembic migration `0001`, thin repository functions, Redis helpers (streams, inbox, locks, token bucket, cancel flag).
- `observability/`: structlog JSON logging with secret redaction and run-context binding; OpenTelemetry span helper.
- docker-compose with pgvector Postgres 16 and Redis 7.
