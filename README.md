# autoswe — Phase 3 (the verification loop)

An autonomous software engineering agent. Give it a repository and a goal; it profiles
the repository, plans, splits the work into a task graph, then edits inside a locked-down
container, runs the tests, commits, pushes `agent/<run-id>` and opens a pull request. It
checkpoints after every step, so a crashed worker resumes instead of starting over, and
it can stop to ask a question when the goal is ambiguous. Every command it runs and every
model turn is a row in Postgres.

Phase 3 made a failing test something it reasons about rather than reports: failures are
parsed into stack frames with a signature that survives an edit, a Debugger must state a
hypothesis before it may change anything, the same failure twice puts it on a different
strategy, and a stuck run escalates instead of looping. Budgets are enforced from the
usage ledger, some commands stop to ask a human, and a cancel lands inside a running
command rather than after it.

**It does not yet review its own diff or scan it for anything** — Phase 4. Read
[TESTING.md Part 6](TESTING.md) before relying on a pull request it opens.

There is a console at **<http://127.0.0.1:8000/>** once the API is up: a list of runs and,
for any one of them, its phases, tasks, live event stream, every tool call and model turn,
and the controls to answer or cancel it. **[TESTING.md](TESTING.md) gets you looking at it
with real data in five minutes, without an API key or any quota.**

The design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); the build plan is in
[docs/PHASES.md](docs/PHASES.md).

## Quick start

```bash
cp .env.example .env                     # then set DATABASE_URL, REDIS_URL, API_KEYS
./scripts/bringup.sh                     # everything: deps, infra, migrations, image, API, worker
```

One idempotent script. It checks the machine first, reads its ports from `.env` rather
than assuming them, prints the evidence for each step, and says plainly when a setting is
missing instead of starting something that will die. `./scripts/bringup.sh status` tells
you what is running; `seed` puts a run on the console with no model and no quota.

The same thing by hand, if you prefer:

```bash
uv sync --all-extras                     # dependencies, including the dev group
make up && make migrate                  # postgres + redis + the agent-install network, then migrations
make sandbox-image                       # build the container the agent works in
make check                               # ruff + mypy + unit tests
make test-int                            # integration tests (needs Docker)
uv run autoswe config                    # non-secret settings; exits 2 naming any missing variable
```

`DATABASE_URL`, `REDIS_URL` and `API_KEYS` are the only settings anything needs;
`LLM_API_KEY` and `GITHUB_TOKEN` are needed by the worker alone, which exits at startup
naming whichever is missing. Set `POSTGRES_PORT` / `REDIS_PORT` in `.env` if 5432 or 6379
are already taken locally, and match `DATABASE_URL` / `REDIS_URL` to them.

## Running an agent

```bash
make api                                 # terminal 1: control plane on 127.0.0.1:8000
make worker                              # terminal 2: the run worker

export AUTOSWE_API_KEY=dev-key-change-me
uv run autoswe run --repo https://github.com/you/some-repo \
  --goal "Implement subtract(a, b) in ops.py so the tests pass. Do not change the tests."
uv run autoswe status <run-id>           # phase: setup -> code -> test -> pr -> done
```

## Which model

Any OpenAI-compatible endpoint works: OpenRouter (the default), vLLM, Ollama or Groq.
Get an OpenRouter key at <https://openrouter.ai/keys> and set:

```bash
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-...
LLM_MODEL=openrouter/free                # auto-router over free models; must support tool calling
```

Prefer `openrouter/free` over a specific `:free` slug: individual free models get withdrawn
without notice and then 404 in the middle of a run.

Two free options that look viable and are not, both measured: **Hugging Face Inference
Providers** cannot serve a single tool-calling request on a free account — the gate is a
pre-flight cost estimate and one tool definition trips it, even on a `$0/$0` provider,
pinned — and **GitHub Models** is retired. `docs/PHASE-2-plan-and-state.md` §1 has the
numbers. OpenRouter's free tier allows 20 requests a minute and 50 a day; the gateway
waits out the per-minute one, but 50 a day is about three runs. Agentic coding is demanding, so a
stronger paid model finishes more runs. `gateway/routing.py` holds the per-role model tiers
used when a provider distinguishes them.

## What the sandbox guarantees

One container per run, torn down afterwards. It runs as a non-root user with every Linux
capability dropped, a read-only root filesystem, a memory, CPU and process cap, and only
the run's worktree bind-mounted. It reaches the network only while dependencies install,
and is disconnected before the agent sees the repository. It never receives your API keys
or database credentials, and git is deliberately unavailable inside it: repository
operations are host-side tools the agent has to call.

## Layout

| Directory | What lives there |
|---|---|
| `core/` | settings and the shared error hierarchy |
| `contracts/` | every structured schema, split into model-facing and runtime models |
| `storage/` | SQLAlchemy models, Alembic migrations, repository functions, the Redis bus |
| `sandbox/` | the sandbox protocol and its Docker implementation |
| `repo/` | clone cache, worktrees, push and pull requests |
| `tools/` | the typed tools an agent may call, and the policy behind them |
| `gateway/` | provider protocol, the OpenAI-compatible provider, routing and pricing |
| `agents/` | agent base, prompts, and the Coder |
| `orchestrator/` | phases, transitions, nodes, the runner and the worker |
| `api/`, `cli/` | control plane and command line |
| `observability/` | structured logging with secret redaction, tracing |

## Testing

```bash
make sandbox-image   # once — without it every Docker-backed test silently skips
make test            # unit: fast, no containers
make test-int        # integration: real Postgres, Redis and Docker; no API key, no spend
make test-e2e        # end to end with a real model; needs LLM_API_KEY, skips without it
```

`make test` should report **446 passed** and `make test-int` **93 passed**, with no skips.
A much lower count with `skipped` in it means the sandbox image is missing and the tests
that matter most are not running.

- **[TESTING.md](TESTING.md)** — start here. `./scripts/bringup.sh` gets the whole product
  running; the rest is a proof for every claim, and a verified list of what is missing.
- [MANUAL-TESTING.md](MANUAL-TESTING.md) — a tour of every capability with the expected
  output beside each command.
- [PHASE-1-CHECKLIST.md](PHASE-1-CHECKLIST.md) — the Phase 1 exit-criteria runbook.
