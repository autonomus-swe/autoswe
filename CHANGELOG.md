# Changelog

## 0.2.0 — Phase 2: plan and state

A run now understands a repository before it edits it. It profiles the repository, plans,
splits the goal into a task graph, checkpoints after every node so a crashed worker
resumes instead of starting over, streams its events live, and stops to ask when the goal
is ambiguous. There is a console to watch it in.

Added
- Read-only tools for the agents that must not write: `read_file` and `search_code`, the
  latter preferring ripgrep and falling back to a Python scan.
- Deterministic repository facts and a ranked repo map. The test command is derived from
  evidence — CI workflow, `pyproject.toml`, `package.json`, `go.mod`, `Makefile` — and
  reports nothing rather than guessing.
- Analyzer, Planner and Decomposer agents, each with a validated contract and one
  correction round.
- `RunState` v2, a checkpoint after every node, and resume that re-attaches the repo
  lock and worktree and replaces the container from the dead attempt.
- Events to a Redis stream and the durable table, `GET /runs/{id}/events` as SSE with
  `Last-Event-ID` replay, `POST /runs/{id}/answer` and `POST /runs/{id}/cancel`.
- A run console at `/`: plain HTML, CSS and JavaScript, no build step. A rail of runs and
  one run in full — phases, tasks, live events, every tool call and model turn, and the
  controls to answer or cancel. Backed by `GET /runs` and `GET /runs/{id}/detail`.
- CLI `watch`, `answer` and `cancel`. `watch` exits non-zero if the run did not pass.
- `scripts/seed_demo_run.py`, which drives a run through every phase with no model, so
  the console can be exercised without spending an allowance.
- `LLM_TIMEOUT_S`. The client timeout was hardcoded at ten minutes and unreachable from
  configuration, which makes a local model unusable — a 7B on CPU needs half an hour for
  one structured-output call.
- `TESTING.md`: five minutes to a console with data in it, then the thorough pass, then
  what the free model tiers actually allow.

Fixed — every one of these was found by running the thing, not by reading it
- Resume destroyed its own work. Refreshing the clone cache pruned the run's own
  unpushed branch, orphaning the worktree on an unborn branch; the run then lost every
  commit it had made and died at push.
- The agent could not install dependencies at all. Every `uv` call in the sandbox
  segfaulted, because `pip install uv` ships a glibc-linked binary and the
  `python:3.12-slim` tag had moved to Debian 13. Both the base image and uv are pinned
  now, and uv comes from Astral's musl build.
- Tool calls were missing from every replay: they were published to Redis only, bypassing
  the table a replay reads from.
- Cancel only landed at node boundaries, so a coder loop could keep editing for minutes
  after someone asked it to stop.
- A submit reminder spent the last turn instead of buying one, so an agent that explored
  to its iteration cap was told to submit with no turn left to do it in.
- Structured output that mirrors the JSON Schema instead of conforming to it is now
  repaired where the fix is unambiguous, rather than discarding a correct answer over its
  wrapper.
- Reads shared the run-creation rate-limit bucket, so the console throttled itself out of
  its own interface within seconds.
- CI ran the integration tier without building the sandbox image, so twelve tests skipped
  while the job reported success; and `gitleaks` had failed on every commit to `main`
  since the repo moved into an organisation, because the action needs a paid licence.

Known gaps
- `cost_usd` records `$0.00` for everything in use: the pricing table only knows
  Anthropic models. Accurate for a free model, wrong in principle, and it must be fixed
  before Phase 3's budgets mean anything.
- The multi-task end-to-end test has not passed in a single run against a real model.
  Both halves are verified separately — the loop completes with a commit per task, and a
  goal decomposes into four or seven tasks — but never together. See
  `docs/PHASE-2-plan-and-state.md` §1 for the attempt log and why.
- The Planner does not pause on a goal as vague as "Add authentication."

## 0.1.0 — Phase 1: single-agent loop

Given a repository URL and a goal in English, an agent clones the repository, edits it
inside a locked-down container, runs the tests, commits, pushes `agent/<run-id>` and
opens a pull request. Every command it runs and every model turn is a row in Postgres.

Added
- Docker sandbox: one container per run, non-root, all capabilities dropped, read-only
  rootfs, memory/CPU/pid caps, only the worktree bind-mounted. Reaches the network only
  while dependencies install, then is disconnected before the agent starts.
- Repository layer: bare clone cache, per-run worktrees, per-process git credentials,
  push restricted to `agent/*`, idempotent pull request creation.
- Tool layer: bash, an editor with view-staleness checks, `run_tests` with a
  pytest-json-report parser, host-side git tools, a deny-list and path confinement.
- LLM gateway over any OpenAI-compatible endpoint, per-role routing, pricing, and
  structured output validated by pydantic.
- Coder agent, the SETUP/CODE/TEST/PR machine with guaranteed teardown, arq worker.
- Control plane: `POST /runs`, `GET /runs/{id}`, `/healthz`, API-key auth with a per-key
  rate limit. CLI `autoswe run` and `autoswe status`.
- Migration 0002: monotonic `seq` on `tool_calls` and `llm_calls` so a run replays in order.
- `CA_BUNDLE` for corporate TLS-inspecting proxies.

Fixed — all found by real runs against live models
- An empty `choices` list on a 200 is retried rather than killing the run.
- An agent that does the work then stops without calling its required tool is reminded.
- Gateway-side rejections (`tool_use_failed`, `output_parse_failed`) are corrected.
- Malformed tool arguments no longer enter the message history, where they poisoned
  every later turn.
- The worker validates its secrets at startup instead of dying mid-run.

## 0.0.1 — Phase 0: foundations

- Project scaffold: uv, ruff, mypy, pytest tiers, Makefile, pre-commit, CI.
- `core/settings.py`: pydantic-settings with fail-fast validation; worker-only secrets.
- `contracts/`: every structured-output schema from README §6 plus runtime wrappers.
- `storage/`: SQLAlchemy 2.0 models for the eight core tables, async Alembic migration `0001`, thin repository functions, Redis helpers (streams, inbox, locks, token bucket, cancel flag).
- `observability/`: structlog JSON logging with secret redaction and run-context binding; OpenTelemetry span helper.
- docker-compose with pgvector Postgres 16 and Redis 7.
