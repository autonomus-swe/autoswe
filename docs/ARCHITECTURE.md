# Autonomous Software Engineering Agent — Architecture

> Give it a goal. It inspects the repository, plans, writes code, runs the tests, debugs failures, reviews its own diff, scans for security issues, and opens a pull request — with every step checkpointed, budgeted, and auditable.

```
"Build a FastAPI authentication service with PostgreSQL, JWT authentication and tests."
                                        ↓
              one command · ~15 minutes · a reviewed, tested, green PR
```

This document is the design reference for the system: what it is built from, why each piece was chosen, how the agents talk to each other, how the verification loop guarantees the PR is actually green, and how to build it in stages.

> **Building it?** The step-by-step build plan lives in [PHASES.md](PHASES.md), with one detailed document per phase (`PHASE-0-foundations.md` … `PHASE-6-interop-and-evals.md`).

---

## Table of contents

1. [Stack decisions (TL;DR)](#1-stack-decisions-tldr)
2. [Design principles](#2-design-principles)
3. [System overview](#3-system-overview)
4. [Components](#4-components)
   - 4.1 [Control plane (FastAPI)](#41-control-plane-fastapi)
   - 4.2 [Orchestrator (typed state machine)](#42-orchestrator-typed-state-machine)
   - 4.3 [Agents](#43-agents)
   - 4.4 [Tool layer](#44-tool-layer)
   - 4.5 [Sandbox (Docker)](#45-sandbox-docker)
   - 4.6 [Repository intelligence](#46-repository-intelligence)
   - 4.7 [LLM gateway](#47-llm-gateway)
   - 4.8 [State: PostgreSQL + Redis](#48-state-postgresql--redis)
   - 4.9 [MCP integration](#49-mcp-integration)
   - 4.10 [Observability and cost](#410-observability-and-cost)
5. [The verification loop](#5-the-verification-loop)
6. [Structured output contracts](#6-structured-output-contracts)
7. [Data model](#7-data-model)
8. [End-to-end walkthrough](#8-end-to-end-walkthrough)
9. [Security model](#9-security-model)
10. [HTTP API](#10-http-api)
11. [Repository layout](#11-repository-layout)
12. [Local development](#12-local-development)
13. [Build order (milestones)](#13-build-order-milestones)
14. [Evaluation](#14-evaluation)
15. [Resume framing](#15-resume-framing)

---

## 1. Stack decisions (TL;DR)

| Concern | Choice | Why | Alternative considered |
|---|---|---|---|
| Language | **Python 3.12** | Best SDK coverage for LLM APIs, tree-sitter, Docker; async-native | — |
| Control plane | **FastAPI + Uvicorn** | Pydantic-native, async, SSE streaming out of the box | Litestar |
| Outer orchestration | **Own typed state machine** (`orchestrator/`) with PostgreSQL checkpoints | ~9 nodes; ~300 lines you can explain line by line; no framework magic; strongest resume story | **LangGraph** (`StateGraph` + `PostgresSaver`) — take it if you want checkpointing/interrupts for free. The agent and tool layers are framework-agnostic so you can swap |
| Inner agent loop | **Anthropic SDK Tool Runner** (`client.beta.messages.tool_runner`) | Handles the request → tool → result loop; per-turn hooks give approval gates, audit logging, and result rewriting | Hand-written `while stop_reason == "tool_use"` loop |
| Primary model | **Claude Opus 5** (`claude-opus-5`), adaptive thinking, effort `xhigh` for coding roles | Strongest long-horizon agentic coding; 1M context | — |
| Cheaper roles | **Claude Sonnet 5** (`claude-sonnet-5`), **Claude Haiku 4.5** (`claude-haiku-4-5`) | Review pre-pass, PR prose, triage, classification | — |
| Open-source model | **Qwen3-Coder** (or DeepSeek-Coder) served by **vLLM** behind an OpenAI-compatible endpoint | Offline/air-gapped fallback, cost experiments, "does not depend on one vendor" story | Ollama for a laptop |
| Structured outputs | **Pydantic v2** + `client.messages.parse(output_format=Model)`; `strict: true` on tool schemas | Plans, task graphs, review and security reports are validated objects, never free text | — |
| Sandbox | **Docker**, one container per run: no network, non-root, read-only rootfs, CPU/memory/pid limits; optional **gVisor** runtime | Agent-generated code and shell commands never touch the host | Firecracker microVMs; Anthropic server-side code execution |
| Code intelligence | **tree-sitter** (symbols), **ripgrep** (search), ranked **repo map** | Language-agnostic, fast, no language server to babysit | LSP servers |
| Semantic retrieval | **pgvector** + code embeddings (Voyage AI or a local open-source embedding model) | Find relevant files for a goal in large repos | Skip for MVP; the repo map is enough for small repos |
| Database | **PostgreSQL 16** (+ pgvector) | Runs, tasks, events, checkpoints, LLM usage, repo index | — |
| Queue / cache / streams | **Redis 7** — `arq` job queue, Redis Streams for live events, locks, rate limits | One dependency covers four needs | Celery + RabbitMQ |
| Git / GitHub | **GitPython** for worktrees and commits; **PyGithub** (or `gh`) for PRs | — | — |
| Static security | **bandit**, **semgrep**, **pip-audit**, **gitleaks** | Deterministic findings first; the LLM pass reads their output | Snyk, Trivy |
| MCP | **`mcp` Python SDK (FastMCP)** — expose the platform as an MCP server; consume GitHub/Postgres MCP servers as agent tools | Interoperable with Claude Code, IDEs, other agents | — |
| Observability | **OpenTelemetry** traces + **Langfuse** (or own `llm_calls` table), **structlog** | Every LLM call and tool call is traceable with token cost | — |
| Tests / tooling | pytest, pytest-asyncio, testcontainers, **uv**, ruff, mypy | — | — |

---

## 2. Design principles

1. **The LLM proposes, the code decides.** Agents produce structured objects and tool calls. Transitions between states (plan → code → test → debug → review) are decided by deterministic Python, never by the model. This is what makes the loop terminate.
2. **Every side effect goes through a typed tool.** Bash exists for breadth, but anything that must be gated, audited, or parsed (edits, tests, commits) is a dedicated tool with a schema.
3. **Nothing the agent executes runs on the host.** All commands run in a per-run container. The host worker only talks to Docker, PostgreSQL, Redis, and the LLM API.
4. **Resumable by construction.** State is checkpointed after every node. A crashed worker resumes from the last checkpoint; a human can pause, inspect, and continue.
5. **Budgets everywhere.** Attempts per task, wall-clock per run, tokens and dollars per run. Exceeding a budget is a normal state transition (`ESCALATE`), not an exception.
6. **Repository content is untrusted input.** READMEs, comments, and test fixtures can contain prompt injections. They are fenced as data, and tools enforce policy independent of what the model was told.
7. **Framework-agnostic core.** Agents and tools depend on a small `LLMProvider` and `Tool` interface, not on LangGraph or on the Anthropic SDK types, so both the orchestrator and the model can be swapped.

---

## 3. System overview

```
                 ┌──────────────────────────────────────────────────────────────┐
                 │                          Clients                             │
                 │      CLI (typer)  ·  Web UI (SSE)  ·  MCP client  ·  CI job   │
                 └────────────────────────────┬─────────────────────────────────┘
                                              │ HTTPS  /  SSE
                 ┌────────────────────────────▼─────────────────────────────────┐
                 │  Control plane — FastAPI                                     │
                 │  POST /runs · GET /runs/{id}/events · POST /runs/{id}/approve │
                 └────────────────────────────┬─────────────────────────────────┘
                                              │ enqueue (Redis / arq)
   ┌──────────────────────────────────────────▼─────────────────────────────────────────┐
   │  Worker — Orchestrator (typed state machine, checkpointed to PostgreSQL)           │
   │                                                                                    │
   │   SETUP → ANALYZE → PLAN → DECOMPOSE → ┌ for each task: CODE → TEST → (DEBUG)* ┐     │
   │                                        └──────────────────────────────────────┘     │
   │                          → REVIEW → SECURITY → PR → DONE                            │
   │                                                                                    │
   │   ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌───────┐ ┌────────┐ ┌────────┐ ┌────────┐  │
   │   │ Planner │ │ Analyzer │ │Decomposer│ │ Coder │ │ Tester │ │Debugger│ │Reviewer│  │
   │   └────┬────┘ └────┬─────┘ └────┬─────┘ └───┬───┘ └───┬────┘ └───┬────┘ └───┬────┘  │
   │        └───────────┴────────────┴───────────┴─────────┴──────────┴──────────┘       │
   │                                             │ tool calls (typed, policy-checked)     │
   │                                    ┌────────▼─────────┐                             │
   │                                    │    Tool layer    │  bash · editor · search      │
   │                                    │                  │  tests · git · ask_user      │
   │                                    └────────┬─────────┘                             │
   └─────────────────────────────────────────────┼──────────────────────────────────────┘
                                                 │ docker exec (timeout, output cap)
                 ┌───────────────────────────────▼──────────┐   ┌──────────────────────────┐
                 │  Sandbox container (one per run)         │   │  LLM gateway             │
                 │  /workspace = git worktree of the repo   │   │  Anthropic SDK (Claude)  │
                 │  no network · non-root · read-only root  │   │  vLLM (open-source)      │
                 └──────────────────────────────────────────┘   └──────────────────────────┘

        ┌──────────────────────┐   ┌──────────────────────┐   ┌──────────────────────────┐
        │  PostgreSQL          │   │  Redis               │   │  Observability           │
        │  runs · tasks ·      │   │  arq queue · Streams │   │  OpenTelemetry · Langfuse │
        │  events · checkpoints│   │  locks · rate limits │   │  cost per run / per call  │
        │  llm_calls · repo idx│   │                      │   │                          │
        └──────────────────────┘   └──────────────────────┘   └──────────────────────────┘
```

**Request path in one sentence:** a client posts a goal, the API stores a `run` and enqueues it, a worker picks it up, clones the repo into a worktree mounted into a fresh container, drives the state machine (each node calls one agent, each agent calls tools that execute in the container), streams events to Redis for the client to watch, and finishes by pushing a branch and opening a PR.

---

## 4. Components

### 4.1 Control plane (FastAPI)

Thin, stateless, horizontally scalable. It never runs agents itself.

Responsibilities:
- Validate and persist run requests (`RunCreate` → `runs` row).
- Enqueue the run on Redis (`arq`).
- Stream run events to clients over **Server-Sent Events**, reading from a Redis Stream (`run:{id}:events`) so any API replica can serve any run.
- Handle human-in-the-loop callbacks: `approve`, `reject`, `answer`, `cancel`. These write to Redis (`run:{id}:inbox`) where the blocked worker is waiting.
- Serve artifacts: the final diff, the review report, the security report, the PR URL.

Auth is an API key in a header, checked by a dependency. Rate limiting is a Redis token bucket keyed by API key, applied to `POST /runs` (this is the expensive endpoint).

### 4.2 Orchestrator (typed state machine)

The orchestrator is the only place that knows the order of operations. It is a plain Python module: a `RunState` Pydantic model, a set of `async def node(state) -> state` functions, and a transition function.

```python
class Phase(StrEnum):
    SETUP = "setup"; ANALYZE = "analyze"; PLAN = "plan"; DECOMPOSE = "decompose"
    CODE = "code"; TEST = "test"; DEBUG = "debug"; REVIEW = "review"
    SECURITY = "security"; PR = "pr"; AWAITING_INPUT = "awaiting_input"
    ESCALATE = "escalate"; DONE = "done"; FAILED = "failed"

class RunState(BaseModel):
    run_id: UUID
    phase: Phase
    goal: str
    repo: RepoProfile | None = None
    plan: ImplementationPlan | None = None
    tasks: TaskGraph | None = None
    current_task_id: str | None = None
    attempts: dict[str, int] = {}          # task_id -> debug attempts used
    last_test_report: TestReport | None = None
    review: ReviewReport | None = None
    security: SecurityReport | None = None
    budget: Budget                          # tokens, dollars, wall-clock, attempts
    usage: Usage = Usage()                  # what has been spent so far
```

Execution model:

```python
async def run(state: RunState) -> RunState:
    while state.phase not in TERMINAL:
        node = NODES[state.phase]                 # e.g. NODES[Phase.CODE] = code_node
        with trace_span(state.phase):
            state = await node(state)             # exactly one agent invocation (or none)
        state.phase = transition(state)           # pure function, no LLM involved
        await checkpoint(state)                   # JSONB row in `checkpoints`
        await emit(state.run_id, PhaseChanged(state.phase))
    return state
```

Key properties:
- **`transition()` is pure and deterministic.** It looks at `last_test_report.passed`, `attempts[task]`, `review.blocking`, `security.critical`, and the budget. The model never picks the next phase.
- **Nodes are idempotent.** Re-running a node after a crash produces the same outcome or a fresh attempt; nothing is double-applied because edits and commits are already in the worktree and git.
- **Interrupts are states.** `ask_user` and any tool flagged `requires_approval` move the run to `AWAITING_INPUT`; the worker parks (releases the job with a lease) until the API writes to the inbox.
- **Resume** loads the latest checkpoint, re-attaches (or recreates) the container, and continues.

**Why not LangGraph?** For nine nodes a hand-rolled machine is smaller than the LangGraph glue would be, fully typed, and trivially testable (`transition()` is a pure function with table-driven tests). If you want persistent checkpointing, `interrupt()`, and streaming without writing them, LangGraph's `StateGraph` + `PostgresSaver` maps one-to-one onto this design: each node becomes a graph node, `transition()` becomes conditional edges. Keep `agents/` and `tools/` free of LangGraph imports either way.

### 4.3 Agents

An agent is a role: a system prompt, a model + effort setting, an allowed tool set, and an output schema. All agents share one `Agent` base class; the differences are configuration.

| Agent | Model / effort | Reads | Produces (schema) | Tools |
|---|---|---|---|---|
| **Repo Analyzer** | Sonnet 5 / medium | file tree, manifests, CI config, README, repo map | `RepoProfile` — language, framework, package manager, test command, lint command, conventions, entry points | read-only: `search_code`, `read_file`, `list_symbols` |
| **Planner** | Opus 5 / high | goal + `RepoProfile` + ranked repo map | `ImplementationPlan` — approach, affected files, new files, risks, test strategy, open questions | read-only |
| **Task Decomposer** | Opus 5 / high | plan + profile | `TaskGraph` — DAG of `Task`s with dependencies, acceptance criteria, files, test selector | none (pure structured output) |
| **Coder** | Opus 5 / **xhigh** | one `Task` + relevant file contents + conventions | edits via tools; returns `TaskResult` (summary, files touched, how to test) | `bash`, `str_replace_based_edit_tool`, `search_code`, `read_file`, `run_tests`, `git_commit` |
| **Tester** | deterministic + Haiku 4.5 / low for triage | test command | `TestReport` — pass/fail, per-test status, parsed failures with stack frames | `run_tests` |
| **Debugger** | Opus 5 / xhigh | failing `TestReport`, source around each frame, task | `DebugHypothesis` then edits; returns `TaskResult` | same as Coder |
| **Reviewer** | Sonnet 5 pre-pass, Opus 5 / high final | full diff + plan + acceptance criteria | `ReviewReport` — findings with severity, `blocking: bool` | read-only |
| **Security** | scanners + Sonnet 5 / medium | diff + bandit/semgrep/gitleaks/pip-audit output | `SecurityReport` — findings, `critical: bool`, checklist status | `run_scanner` |
| **PR Writer** | Sonnet 5 / low | commits, plan, reports | `PullRequestDescription` | `git_log`, `git_diff` |

Design notes:
- **Planner and Analyzer are read-only** by construction: their tool set has no mutating tools. That is enforced by the tool layer, not by the prompt.
- **Coder works one task at a time** with a fresh context per task: system prompt + `RepoProfile` + the task + the files the decomposer named. It does not see the whole run history. This keeps context small, cache hits high, and failures isolated.
- **Debugger is a separate role, not "Coder again."** It gets a different prompt (form a hypothesis before editing, re-run only the failing tests first, then the full suite) and its output includes the hypothesis so failed attempts are diagnosable.
- **Reviewer runs in two passes**: a cheap Sonnet 5 pass over the diff produces candidate findings; Opus 5 verifies each candidate against the code and decides `blocking`. This is the same review-then-verify pattern used by production review agents and cuts false positives.
- **Security is scanners first, LLM second.** The model's job is to read scanner output plus the diff and decide what is real, what is a false positive, and what the scanners cannot see (auth logic, missing rate limits, PII in logs).

### 4.4 Tool layer

Tools are the boundary between "what the model wants" and "what the harness allows." Each tool declares its policy, and the tool runner's per-turn hook enforces it before execution.

```python
class Tool(Protocol):
    name: str
    description: str
    input_schema: dict            # JSON Schema; strict
    mutating: bool                # touches workspace, git, or network
    parallel_safe: bool           # read-only tools can run concurrently
    requires_approval: bool       # blocks the run in AWAITING_INPUT
    async def __call__(self, ctx: RunContext, **kwargs) -> ToolResult: ...
```

| Tool | Kind | Notes |
|---|---|---|
| `bash` | Anthropic-defined `bash_20250124` | Runs in the sandbox with a timeout. Deny-list for destructive patterns (`rm -rf /`, `git push --force`, `curl \| sh`) as defense in depth, on top of the sandbox having no network and no host access. Output capped (head + tail) with an explicit truncation marker. |
| `str_replace_based_edit_tool` | Anthropic-defined `text_editor_20250728` | `view`, `create`, `str_replace`, `insert`. The harness stores a content hash on every `view`; an edit is rejected if the file changed since the model last viewed it (staleness check bash cannot enforce). |
| `read_file` | custom, read-only | With line ranges; returns numbered lines. |
| `search_code` | custom, read-only, parallel-safe | ripgrep wrapper: pattern + glob + max results. |
| `list_symbols` | custom, read-only, parallel-safe | tree-sitter: functions, classes, methods with line ranges for a file. |
| `run_tests` | custom, mutating (creates artifacts) | Runs the repo's test command with JUnit XML / `pytest --json-report`; returns a **parsed** `TestReport`, not raw stdout. Supports a selector to run only failing tests. |
| `run_scanner` | custom | Runs bandit / semgrep / gitleaks / pip-audit; returns parsed findings. |
| `git_status`, `git_diff`, `git_log` | custom, read-only | — |
| `git_commit` | custom, mutating | Commits with a conventional message; branch is always `agent/<run-id>`; `push` is not a tool — the orchestrator pushes in the `PR` node. |
| `ask_user` | custom, `requires_approval` | Moves the run to `AWAITING_INPUT` and returns the human's answer when it arrives. |

**Bash vs. dedicated tools rule:** start with bash for breadth, promote an action to a dedicated tool when you need to gate it, audit it, parse its output, or run it in parallel. Tests, edits, and commits are promoted; `ls` and `cat` are not.

**Parallel tool calls:** the model may emit several tool calls in one turn. Read-only, parallel-safe tools run concurrently; the results are returned together in one message. Mutating tools are serialized.

### 4.5 Sandbox (Docker)

One container per run, created in `SETUP`, destroyed in `DONE`/`FAILED` (or kept for a debug TTL).

```
docker run -d \
  --name run-<id> \
  --user 1000:1000 \
  --read-only --tmpfs /tmp:size=1g \
  --mount type=bind,src=/var/agent/worktrees/<id>,dst=/workspace \
  --network none \
  --memory 4g --cpus 2 --pids-limit 512 \
  --cap-drop ALL --security-opt no-new-privileges \
  [--runtime runsc]                # gVisor, optional
  agent-sandbox:python-3.12
```

- **Image**: per-stack base images (`python-3.12`, `node-20`, `go-1.23`) pre-baked with git, ripgrep, build tools, and the common test runners so most runs need no installs.
- **Dependencies**: `SETUP` runs the install step with the container attached to a restricted network whose only egress is a caching proxy allow-listing PyPI/npm/Go proxy. After install, `docker network disconnect` drops it. Everything the agents do afterwards runs with **no network**.
- **Execution**: the worker calls `container.exec_run` (Docker SDK) wrapped in `timeout <s>`, with stdout/stderr captured, a byte cap, and the exit code. There is no daemon inside the container and no SSH.
- **Filesystem**: `/workspace` is the only writable path and is a bind-mounted **git worktree** on the worker host. This gives the harness a real diff at any moment and makes cleanup a `git worktree remove`.
- **Secrets**: none. No API keys, no GitHub token, no `.env` from the operator enter the container. The GitHub token is used only by the worker process for the final push.
- **Escape hatch for heavier isolation**: swap the Docker runtime for gVisor (`runsc`) with one flag, or replace the `Sandbox` implementation with Firecracker/E2B — the tool layer talks to a `Sandbox` interface (`exec`, `read`, `write`, `snapshot`), not to Docker directly.

### 4.6 Repository intelligence

The planner cannot read a 5,000-file repo. It reads a **repo profile** and a **ranked repo map**.

1. **Profile** (deterministic, before any LLM call): detect language(s) and frameworks from manifests (`pyproject.toml`, `package.json`, `go.mod`), find the test command (pytest config, `npm test`, CI workflow), lint config, entry points, and the directory layout. The Analyzer agent fills the parts that need judgment (conventions, "how do they structure services").
2. **Symbol index**: tree-sitter parses every source file into definitions (module → class → function, with signatures and line ranges) and references. Stored in `repo_symbols` keyed by commit SHA, so re-runs on the same SHA are free.
3. **Repo map**: files are ranked by (a) graph centrality on the import/reference graph and (b) lexical relevance of their symbols to the goal (BM25). The top-K files are rendered as a compact outline — path plus signatures, no bodies — and fit in 2–4K tokens. This is what the Planner and Decomposer see.
4. **Semantic retrieval (optional, M5)**: embed symbol chunks into pgvector; `search_code` gains a `semantic=true` mode for "where is rate limiting handled" style questions.

### 4.7 LLM gateway

A single module every agent goes through. It owns model routing, caching, budgets, retries, and usage accounting.

```python
class LLMProvider(Protocol):
    async def parse[T](self, req: Request, output: type[T]) -> Parsed[T]: ...   # structured
    def tool_runner(self, req: Request, tools: list[Tool], hooks: Hooks) -> Runner: ...
```

**`AnthropicProvider`** (official `anthropic` SDK):
- `thinking={"type": "adaptive"}` everywhere; `output_config={"effort": ...}` per role (`xhigh` for Coder/Debugger, `high` Planner/Reviewer, `medium`/`low` for cheap roles).
- **Structured outputs** via `client.messages.parse(..., output_format=PydanticModel)` → `response.parsed_output` is a validated instance. Tool schemas use `strict: true`.
- **Tool runner** via `client.beta.messages.tool_runner(...)` for Coder/Debugger; per-turn hooks implement the policy checks in §4.4, audit logging, and result truncation.
- **Prompt caching**: `cache_control: {"type": "ephemeral"}` breakpoints on the static prefix (tools + system prompt) and a second one after the per-run `RepoProfile` + repo map. Volatile content (task, timestamps) goes after. `usage.cache_read_input_tokens` is recorded per call so a zero hit rate is visible.
- **Long coder sessions**: context editing (`clear_tool_uses_20250919`) prunes stale tool results; server-side **compaction** (beta) handles sessions that approach the window — `response.content` is appended back verbatim so compaction blocks survive.
- **Task budgets** (beta): the Coder gets a token budget for the task so it paces itself instead of being cut off by `max_tokens`.
- **Streaming** for every call with large `max_tokens`; events are forwarded to the run's Redis Stream so the UI shows the agent thinking (with `display: "summarized"`).
- **Refusals**: `stop_reason == "refusal"` is handled explicitly; server-side fallbacks are enabled so a classifier refusal routes to a fallback model instead of failing the run.

**`OpenAICompatProvider`** (vLLM serving Qwen3-Coder / DeepSeek-Coder):
- Same interface. Structured output via JSON-schema-constrained decoding (vLLM guided decoding); tool calls via the model's native tool-call format.
- Used for: offline mode, cost experiments, and as the executor for simple tasks when a run's dollar budget is nearly spent.
- Claude never goes through this path; it uses the official SDK only.

**Model routing policy** (`gateway/routing.py`):

```python
ROUTES = {
    "planner":   Route(model="claude-opus-5",   effort="high"),
    "decompose": Route(model="claude-opus-5",   effort="high"),
    "coder":     Route(model="claude-opus-5",   effort="xhigh"),
    "debugger":  Route(model="claude-opus-5",   effort="xhigh"),
    "analyzer":  Route(model="claude-sonnet-5", effort="medium"),
    "review_pre":Route(model="claude-sonnet-5", effort="medium"),
    "review":    Route(model="claude-opus-5",   effort="high"),
    "security":  Route(model="claude-sonnet-5", effort="medium"),
    "triage":    Route(model="claude-haiku-4-5"),
    "pr_writer": Route(model="claude-sonnet-5", effort="low"),
}
```

Routing is per role, not per run, so each role keeps its own cache namespace. Cost per completed task is the metric; a cheaper request that needs more retries is not cheaper.

### 4.8 State: PostgreSQL + Redis

**PostgreSQL** is the system of record:
- `runs`, `tasks`, `steps` (one per agent invocation), `tool_calls`, `events`, `checkpoints`, `artifacts`, `llm_calls`, `repo_symbols`, `repo_embeddings` (pgvector). Full DDL in §7.
- Checkpoints are JSONB snapshots of `RunState` written after every node; the latest one is the resume point.
- `llm_calls` stores model, tokens (input, output, cache read, cache write), cost, latency, and `stop_reason` for every request. Budgets are enforced from this table, not from in-memory counters.

**Redis** is the nervous system:
- `arq` job queue for runs (retries, leases, dead-letter).
- Redis Streams `run:{id}:events` for live SSE fan-out (append-only, replayable, trimmed by length).
- `run:{id}:inbox` list for human replies; the worker `BLPOP`s with a timeout while in `AWAITING_INPUT`.
- Locks: one run per repo+branch at a time (`SET NX` with TTL).
- Rate limiting: token bucket per API key.
- Cache: rendered repo maps keyed by `sha:goal_hash` (short TTL).

### 4.9 MCP integration

Model Context Protocol appears in two directions.

**As a server** (`mcp/server.py`, FastMCP): the platform exposes `create_run`, `get_run`, `list_events`, `search_code`, `read_file` as MCP tools. Claude Code, Claude Desktop, an IDE, or another agent can drive the system ("kick off a run for issue #42 and tell me when the PR is up") without knowing the HTTP API.

**As a client** (`mcp/client.py`): agents can mount external MCP servers as tools:
- **GitHub MCP** — read the issue that motivated the goal, list existing PRs, post the PR.
- **PostgreSQL MCP** (read-only) — let the Analyzer inspect the target service's schema when the goal touches the database.
- The `mcp` client lists the server's tools and bridges them into the tool runner alongside local tools; the same policy flags (`mutating`, `requires_approval`) apply.

For URL-reachable servers, the Claude API's native MCP connector (`mcp_servers` + `mcp_toolset`) is an alternative that moves the round-trip server-side; the bridged client is the default because it keeps policy enforcement in the harness.

### 4.10 Observability and cost

- **Tracing**: one OpenTelemetry trace per run; spans per phase, per agent step, per LLM call, per tool call, per sandbox exec. Exported to Langfuse (LLM-aware UI: prompts, completions, tokens, cost) and/or any OTLP backend.
- **Structured logs** (`structlog`, JSON) with `run_id`, `task_id`, `step_id` on every line. Repository content and diffs are logged as artifacts, not as log lines.
- **Cost**: `llm_calls` is aggregated into `runs.cost_usd` live; the UI shows spend vs. budget; `ESCALATE` fires at 90 % of the budget so the run can finish cleanly.
- **Metrics** (Prometheus): runs by outcome, attempts per task, test pass rate after first coder pass, debug success rate, tokens per solved task, cache hit rate, sandbox exec time.

---

## 5. The verification loop

This is the heart of the system. Written out as the transition table the orchestrator implements.

```
                    ┌──────────────────────┐
                    │  CODE (task t)       │◄───────────────────────────────┐
                    └──────────┬───────────┘                                │
                               ▼                                            │
                    ┌──────────────────────┐                                │
                    │  TEST  (selector t,  │                                │
                    │  then full suite)    │                                │
                    └──────────┬───────────┘                                │
                               ▼                                            │
                     passed? ──yes──► next task ──(none left)──► REVIEW      │
                        │                                          │        │
                        no                                    blocking? ─yes─┘ (fix loop, max 2)
                        ▼                                          │
              attempts[t] < 3 ? ──no──► ESCALATE                   no
                        │               (replan task, or          ▼
                        yes              ask_user, or FAIL)     SECURITY
                        ▼                                          │
                    ┌──────────────────────┐                  critical? ─yes─► CODE (fix loop, max 2)
                    │  DEBUG (hypothesis   │                       │
                    │  → patch)            │                       no
                    └──────────┬───────────┘                       ▼
                               └──────────► TEST                  PR ──► DONE
```

```python
def transition(s: RunState) -> Phase:
    if s.budget.exceeded(s.usage):            return Phase.ESCALATE
    match s.phase:
        case Phase.SETUP:      return Phase.ANALYZE
        case Phase.ANALYZE:    return Phase.PLAN
        case Phase.PLAN:       return Phase.AWAITING_INPUT if s.plan.open_questions else Phase.DECOMPOSE
        case Phase.DECOMPOSE:  return Phase.CODE
        case Phase.CODE:       return Phase.TEST
        case Phase.TEST:
            if s.last_test_report.passed:
                return Phase.CODE if s.tasks.next_ready() else Phase.REVIEW
            if s.attempts[s.current_task_id] >= MAX_DEBUG_ATTEMPTS:   # 3
                return Phase.ESCALATE
            if s.last_test_report.signature == s.previous_failure_signature:
                s.strategy = "alternative"        # same failure twice → tell the debugger to change approach
            return Phase.DEBUG
        case Phase.DEBUG:      return Phase.TEST
        case Phase.REVIEW:     return Phase.CODE if s.review.blocking and s.fix_rounds < 2 else Phase.SECURITY
        case Phase.SECURITY:   return Phase.CODE if s.security.critical and s.fix_rounds < 2 else Phase.PR
        case Phase.PR:         return Phase.DONE
```

**Budgets and exit conditions**

| Budget | Default | On exceed |
|---|---|---|
| Debug attempts per task | 3 | `ESCALATE`: decomposer re-plans that task once; if it fails again, `ask_user`; if no user, `FAILED` with a partial PR marked draft |
| Review/security fix rounds | 2 each | Proceed to PR with the findings listed under "Known issues" and the PR marked draft |
| Wall-clock per run | 45 min | `ESCALATE` → draft PR with progress so far |
| Dollars per run | configurable (e.g. $10) | At 90 %: non-critical roles downgrade to Sonnet 5; at 100 %: `ESCALATE` |
| No-progress detection | same failure signature twice | Debugger is told its previous hypothesis was wrong and must try a different one |

**Tester details.** `run_tests` first runs only the task's selector (fast signal), then the full suite (regression guard). Output is parsed from JUnit XML / `pytest --json-report` into `TestReport`; stack traces are parsed into frames (`file`, `line`, `function`, `code`) so the Debugger receives structured data plus the 20 lines of source around each in-repo frame — not 400 lines of raw stdout.

**Which failures are the agent's.** A real repository has failing tests before an agent arrives, and a run blamed for inheriting them never finishes. So the full suite is run once at the end of `ANALYZE` — after the test command stops being a guess, and after the network is disconnected, so the baseline is taken under the conditions the agent will face — and every signature it produces is excused later. Three rules decide the rest:

- **The selector is never excused.** It names the tests the task was written against, so a failure there is the job, not an inheritance. The targeted run also short-circuits: the suite is not informative while the task's own tests fail.
- **Without a selector, nothing is excused.** There is no line between "already broken" and "what I was asked to fix", and the tests a goal names are failing before it starts — that is what makes it a goal. Applying a baseline here is how a run reports success having done nothing.
- **A test that fails in the suite and passes alone is recorded, not forgotten.** It is either flaky or order-dependent and one re-run cannot say which, so the run continues and the pull request lists it. Same for the pre-existing failures: `RunState.flaky_tests` and `preexisting_failures` exist so a reviewer told "all tests pass" can see which ones were not made to pass here.

The unfiltered report is kept as a `test_report_raw` artifact whenever filtering changed the verdict, because "why did this pass" is a question a reviewer is entitled to answer. The baseline is cached in Redis by `repo:base_sha` — a property of the commit, not of the run that discovered it — so repeated runs on the same commit skip a full suite. A cache miss costs time and is never wrong.

One model call happens in this phase, and only when the parser produced a failure it could not describe at all: the Tester (Haiku 4.5) puts a class to it. The answer is rendered as a note in the Debugger's context and **never written back onto `TestFailure.kind`**, because `kind` feeds the signature that no-progress detection compares. A label that can come back differently on an identical report would make three identical attempts look like three different ones.

**Debugger details.** Its prompt forces a `DebugHypothesis` (failure class: assertion / exception / import / environment / flaky; root cause; plan) *before* it is allowed to edit. The hypothesis is stored on the step, so when a run escalates a human can read three hypotheses and see where it went wrong.

---

## 6. Structured output contracts

Every agent-to-orchestrator handoff is a Pydantic model. These are the actual contracts (abbreviated).

```python
class RepoProfile(BaseModel):
    languages: list[str]
    framework: str | None
    package_manager: str
    test_command: str
    lint_command: str | None
    conventions: list[str]              # "services in app/services, one class per file", ...
    entry_points: list[str]

class ImplementationPlan(BaseModel):
    approach: str
    affected_files: list[str]
    new_files: list[str]
    risks: list[str]
    test_strategy: str
    open_questions: list[str]           # non-empty → run pauses in AWAITING_INPUT

class Task(BaseModel):
    id: str
    title: str
    description: str
    depends_on: list[str]
    files: list[str]
    acceptance_criteria: list[str]
    test_selector: str                  # e.g. "tests/test_auth.py"

class TaskGraph(BaseModel):
    tasks: list[Task]
    def next_ready(self) -> Task | None: ...   # topological order, all deps DONE

class TestFailure(BaseModel):
    test_id: str
    kind: Literal["assertion", "exception", "import", "environment", "timeout"]
    message: str
    frames: list[Frame]                 # file, line, function, code
    signature: str                      # hash(test_id, kind, top in-repo frame)

class TestReport(BaseModel):
    passed: bool
    total: int; failed: int; errors: int
    failures: list[TestFailure]
    duration_s: float

class DebugHypothesis(BaseModel):
    failure_class: str
    root_cause: str
    plan: str
    confidence: float

class ReviewFinding(BaseModel):
    file: str; line: int
    severity: Literal["blocking", "major", "minor", "nit"]
    category: str                       # correctness, security, tests, style, ...
    summary: str
    failure_scenario: str               # concrete input → wrong output

class ReviewReport(BaseModel):
    findings: list[ReviewFinding]
    blocking: bool

class SecurityReport(BaseModel):
    findings: list[SecurityFinding]     # tool, rule, file, line, severity, verified_by_llm
    critical: bool
    checklist: dict[str, bool]          # no_secrets, inputs_validated, parameterized_sql, ...

class PullRequestDescription(BaseModel):
    title: str
    summary: str
    changes: list[str]
    testing: str
    known_issues: list[str]
    rollback: str
```

Obtaining one is one call:

```python
plan = (await provider.parse(req, ImplementationPlan)).parsed_output
```

Because the schemas are enforced by the API (`output_config.format` under the hood), the orchestrator never parses free text and never sees a malformed plan.

---

## 7. Data model

```sql
create table runs (
  id uuid primary key,
  repo_url text not null,  base_branch text not null,  work_branch text not null,
  goal text not null,
  phase text not null,                 -- Phase enum
  status text not null,                -- queued | running | awaiting_input | done | failed | cancelled
  budget jsonb not null,  cost_usd numeric(10,4) not null default 0,
  pr_url text,
  created_at timestamptz default now(), updated_at timestamptz default now()
);

create table tasks (
  id text, run_id uuid references runs(id),
  title text, description text, depends_on text[], files text[],
  acceptance_criteria text[], test_selector text,
  status text not null,                -- pending | in_progress | done | failed
  attempts int default 0,
  primary key (run_id, id)
);

create table steps (                    -- one row per agent invocation
  id uuid primary key, run_id uuid, task_id text,
  agent text not null, phase text not null,
  input jsonb, output jsonb,           -- the structured contract in/out
  started_at timestamptz, finished_at timestamptz, error text
);

create table tool_calls (
  id uuid primary key, step_id uuid references steps(id),
  name text, input jsonb, output_preview text, exit_code int,
  duration_ms int, approved_by text
);

create table llm_calls (
  id uuid primary key, step_id uuid references steps(id),
  provider text, model text, effort text,
  input_tokens int, output_tokens int, cache_read_tokens int, cache_write_tokens int,
  cost_usd numeric(10,6), latency_ms int, stop_reason text
);

create table checkpoints (
  run_id uuid, seq int, state jsonb not null, created_at timestamptz default now(),
  primary key (run_id, seq)
);

create table events (                   -- durable copy of the Redis stream
  id bigserial primary key, run_id uuid, ts timestamptz default now(),
  type text, payload jsonb
);

create table artifacts (
  id uuid primary key, run_id uuid, kind text,   -- diff | test_report | review | security | pr
  path text, content jsonb
);

create table repo_symbols (
  repo_sha text, path text, kind text, name text, signature text,
  start_line int, end_line int, refs text[]
);

create table repo_embeddings (
  repo_sha text, path text, chunk_id int, embedding vector(1024), text text
);
```

---

## 8. End-to-end walkthrough

Goal: **"Build a FastAPI authentication service with PostgreSQL, JWT authentication and tests."** Target: an existing (possibly empty) repo.

| # | Phase | What happens | Output |
|---|---|---|---|
| 1 | `SETUP` | Clone repo, create worktree on `agent/<run-id>`, start sandbox, install deps through the allow-listed proxy, disconnect network. | container ready |
| 2 | `ANALYZE` | Deterministic profile finds `pyproject.toml`, pytest, no framework yet. Analyzer confirms conventions (or proposes standard ones for an empty repo). | `RepoProfile{test_command="pytest -q", package_manager="uv"}` |
| 3 | `PLAN` | Planner reads goal + profile + repo map. | `ImplementationPlan`: SQLAlchemy async models, `users` table with Alembic migration, `/auth/register`, `/auth/login`, `/auth/me`; JWT via `PyJWT` with HS256 from env; password hashing with argon2; rate limit login; tests with testcontainers-postgres. `open_questions=[]` → no pause. |
| 4 | `DECOMPOSE` | Task graph: **T1** project skeleton + settings (env validation) → **T2** DB models + migration → **T3** password + JWT utilities → **T4** register/login/me routes → **T5** rate limiting + error handling → **T6** integration tests. Each task has acceptance criteria and a `test_selector`. | `TaskGraph` (6 tasks) |
| 5 | `CODE` T1 | Coder creates `app/main.py`, `app/settings.py` (pydantic-settings, fails fast on missing `DATABASE_URL`/`JWT_SECRET`), `tests/conftest.py`. Commits. | `TaskResult` |
| 6 | `TEST` T1 | `pytest tests/test_settings.py` then full suite → green. | `TestReport{passed=true}` |
| 7 | `CODE`/`TEST` T2, T3 | Same loop. T3 test fails: `jwt.exceptions.InvalidAlgorithmError` — frames parsed, source attached. | `TestReport{passed=false, signature=...}` |
| 8 | `DEBUG` T3 | Hypothesis: "algorithms list not passed to `jwt.decode`". Patch. Re-run failing test, then full suite → green. `attempts[T3]=1`. | `DebugHypothesis` + `TaskResult` |
| 9 | `CODE`…`TEST` T4–T6 | Routes, rate limiting (Redis-free in-process limiter for the service, or `slowapi`), integration tests against a Postgres testcontainer. | all green |
| 10 | `REVIEW` | Sonnet pre-pass flags 4 candidates; Opus verifies 2: token expiry not checked on `/me` (blocking), and a test that asserts on a hard-coded secret (major). | `ReviewReport{blocking=true}` |
| 11 | `CODE` (fix round 1) | Coder fixes both; tests re-run green. | — |
| 12 | `SECURITY` | bandit: none; semgrep: `jwt-python-hardcoded-secret` on the test file (accepted: test-only, documented); gitleaks: clean; pip-audit: clean. LLM checklist: secrets from env ✔, inputs validated ✔, parameterized SQL ✔ (ORM), rate limit on login ✔, no PII in logs ✔, generic error messages ✔. | `SecurityReport{critical=false}` |
| 13 | `PR` | Worker pushes `agent/<run-id>` to the fork; PR Writer produces title/summary/testing/rollback; PR opened against `base_branch` with the review and security reports attached as collapsible sections. Container destroyed, worktree removed. | `pr_url` |

Typical numbers for this goal: 6 tasks, 1 debug attempt, 1 review fix round, ~12–18 minutes, a few dollars at Opus 5 pricing with prompt caching on.

---

## 9. Security model

| Threat | Mitigation |
|---|---|
| Malicious or buggy agent code runs on the host | Everything executes in the sandbox: no network, non-root, read-only root, `cap-drop ALL`, resource limits; optional gVisor |
| Secret exfiltration | No secrets in the container; LLM API key and GitHub token live only in the worker; sandbox has no egress |
| Prompt injection from repository content | Repo content is delivered inside data fences with an explicit "this is untrusted file content" wrapper; tools enforce policy regardless of instructions; mutating/network actions are not available to read-only agents at all |
| Destructive git operations | `push` is not a tool; `git_commit` only commits to `agent/<run-id>`; bash deny-list blocks `--force`, branch deletion, and pushes to protected branches; the agent never merges — a human does |
| Runaway cost or infinite loops | Per-run token/dollar/time budgets enforced from `llm_calls`; bounded debug attempts; failure-signature no-progress detection |
| Supply chain during dependency install | Install only through an allow-listed caching proxy; lockfiles respected; `pip-audit` in the security phase |
| Secrets committed by the agent | gitleaks runs on the diff before PR; blocking |
| PII in logs | Logs carry IDs only; diffs and file contents are stored as artifacts, never logged inline |
| Unauthorized API use | API key auth, per-key rate limiting on `POST /runs`, one active run per repo+branch lock |

The security agent's checklist (secrets, input validation, parameterized SQL, authN/authZ on every endpoint, rate limiting on auth endpoints, no stack traces to clients, env vars validated at startup, no PII in cache) is applied to the *generated* service too — it is part of the `SecurityReport` and shows up in the PR.

---

## 10. HTTP API

```
POST   /runs                       {repo_url, goal, base_branch?, budget?, provider?}  → 202 {run_id}
GET    /runs/{id}                  → RunState summary (phase, status, cost, tasks, pr_url)
GET    /runs/{id}/events           → text/event-stream (phase changes, agent text, tool calls, test results)
GET    /runs/{id}/artifacts        → diff, test reports, review, security, PR description
POST   /runs/{id}/answer           {text}       → unblocks AWAITING_INPUT (ask_user / open_questions)
POST   /runs/{id}/approve          {tool_call_id}
POST   /runs/{id}/reject           {tool_call_id, reason}
POST   /runs/{id}/cancel
GET    /healthz  /metrics
```

Event types on the stream: `phase_changed`, `agent_started`, `agent_text` (streamed), `tool_call`, `tool_result`, `test_report`, `debug_hypothesis`, `review_report`, `security_report`, `awaiting_input`, `pr_opened`, `budget_warning`, `run_finished`.

---

## 11. Repository layout

```
autoswe/
├── api/                    # FastAPI control plane
│   ├── main.py             # app factory, middleware, routers
│   ├── routes/runs.py      # /runs endpoints + SSE
│   ├── auth.py             # API-key dependency, rate limiting
│   └── schemas.py          # request/response models
├── orchestrator/
│   ├── state.py            # RunState, Phase, Budget
│   ├── nodes.py            # one async function per phase
│   ├── transition.py       # pure transition() — table-driven tests live next to it
│   ├── runner.py           # loop, checkpointing, resume, interrupts
│   └── worker.py           # arq worker entrypoint
├── agents/
│   ├── base.py             # Agent: prompt + route + tools + output schema
│   ├── prompts/            # one .md per role
│   ├── analyzer.py planner.py decomposer.py coder.py tester.py
│   ├── debugger.py reviewer.py security.py pr_writer.py
├── tools/
│   ├── base.py             # Tool protocol, policy flags, registry
│   ├── bash.py editor.py fs.py search.py symbols.py
│   ├── tests.py            # run_tests + JUnit/json-report parsing → TestReport
│   ├── git.py scanners.py ask_user.py
│   └── policy.py           # deny-lists, approval gating, staleness checks
├── sandbox/
│   ├── base.py             # Sandbox interface: exec/read/write/snapshot
│   ├── docker.py           # Docker implementation
│   └── images/             # Dockerfiles per stack
├── repo/
│   ├── clone.py worktree.py profile.py
│   ├── symbols.py          # tree-sitter indexing
│   ├── repomap.py          # ranking + rendering
│   └── embeddings.py       # pgvector (optional)
├── gateway/
│   ├── provider.py         # LLMProvider protocol
│   ├── anthropic_provider.py
│   ├── openai_compat_provider.py   # vLLM / open-source models
│   ├── routing.py          # role → model/effort
│   ├── caching.py          # breakpoint placement helpers
│   └── budget.py           # usage accounting, enforcement
├── contracts/              # all Pydantic schemas from §6
├── storage/
│   ├── models.py           # SQLAlchemy models (§7)
│   ├── migrations/         # Alembic
│   └── redis.py            # streams, inbox, locks, rate limits
├── mcp/
│   ├── server.py           # FastMCP: expose the platform
│   └── client.py           # mount external MCP servers as tools
├── observability/          # OTel setup, Langfuse exporter, structlog config, metrics
├── cli/                    # typer: `autoswe run --repo ... --goal ...`, `autoswe watch <id>`
├── evals/                  # SWE-bench Lite harness + own task suite
├── tests/                  # unit (transition table, parsers, policy), integration (testcontainers), e2e
├── docker-compose.yml
├── pyproject.toml
└── README.md
```

---

## 12. Local development

```yaml
# docker-compose.yml (abbreviated)
services:
  api:     { build: ., command: uvicorn api.main:app --host 0.0.0.0 --port 8000, env_file: .env, depends_on: [postgres, redis] }
  worker:  { build: ., command: arq orchestrator.worker.WorkerSettings, env_file: .env,
             volumes: [/var/run/docker.sock:/var/run/docker.sock, worktrees:/var/agent/worktrees],
             depends_on: [postgres, redis] }
  postgres: { image: pgvector/pgvector:pg16, environment: { POSTGRES_PASSWORD: dev }, ports: ["5432:5432"] }
  redis:    { image: redis:7-alpine, ports: ["6379:6379"] }
  proxy:    { image: agent-egress-proxy, environment: { ALLOW: "pypi.org,files.pythonhosted.org,registry.npmjs.org" } }
  langfuse: { image: langfuse/langfuse, ports: ["3000:3000"], depends_on: [postgres] }   # optional
  vllm:     { image: vllm/vllm-openai, command: --model Qwen/Qwen3-Coder-30B-A3B-Instruct, deploy: { resources: { reservations: { devices: [{ capabilities: [gpu] }] } } } }  # optional
volumes: { worktrees: {} }
```

```bash
uv sync                                  # deps
cp .env.example .env                     # ANTHROPIC_API_KEY, GITHUB_TOKEN, DATABASE_URL, REDIS_URL
docker compose up -d postgres redis proxy
uv run alembic upgrade head
docker build -t agent-sandbox:python-3.12 sandbox/images/python
uv run uvicorn api.main:app --reload &   # control plane
uv run arq orchestrator.worker.WorkerSettings &
uv run autoswe run --repo https://github.com/you/some-repo --goal "Build a FastAPI authentication service with PostgreSQL, JWT authentication and tests."
uv run autoswe watch <run-id>            # tails the SSE stream
```

Required environment variables are validated at startup by `pydantic-settings`; the process exits with the missing variable's name if any are absent.

---

## 13. Build order (milestones)

Each milestone is demoable on its own. Do not start M2 until M1 opens a PR.

| Milestone | Scope | Demo |
|---|---|---|
| **M1 — Single-agent loop** | FastAPI `POST /runs`, arq worker, Docker sandbox, git worktree, Coder with `bash` + editor + `run_tests`, push branch + PR. No planner, no debugger. | "It edited the repo, tests pass, here is the PR." |
| **M2 — Plan and state** | Planner + Decomposer with structured outputs, `RunState` checkpoints in Postgres, resume after crash, SSE events. | Kill the worker mid-run; restart; it continues. |
| **M3 — Verification loop** | Tester with parsed `TestReport`, Debugger with hypotheses, attempt budgets, no-progress detection, `ESCALATE`, `ask_user`. | Inject a failing test; watch it debug and recover. |
| **M4 — Review, security, PR quality** | Two-pass Reviewer, scanner-backed Security agent, fix rounds, PR Writer with reports attached, gitleaks gate. | A PR with a review and security section a human would accept. |
| **M5 — Scale and cost** | tree-sitter repo map, pgvector retrieval, prompt caching breakpoints, context editing + compaction, task budgets, cost dashboard, model routing by budget. | Run on a 3,000-file repo under $5. |
| **M6 — Interop and independence** | MCP server + client, `OpenAICompatProvider` with vLLM + Qwen3-Coder, eval harness on SWE-bench Lite, CLI polish. | Drive it from Claude Code over MCP; run offline on an open model. |

---

## 14. Evaluation

Ship numbers, not adjectives.

- **Benchmarks**: SWE-bench Lite (or a Verified subset) plus a private suite of 20–30 tasks across your own repos (feature, bug fix, refactor).
- **Metrics per task**: resolved (tests pass) · debug attempts · review fix rounds · wall-clock · tokens and dollars · cache hit rate.
- **Ablations worth a paragraph in the README**: with/without Debugger; with/without repo map; Opus 5 vs Sonnet 5 as Coder; Claude vs Qwen3-Coder; effort `high` vs `xhigh` on the Coder.
- **Harness**: `evals/run.py` executes tasks through the real API (no shortcuts), stores results in `evals/results/*.jsonl`, and renders a table. Use the Message Batches API for LLM-as-judge scoring of PR descriptions at half price.

---

## 15. Resume framing

> Built an autonomous software-engineering agent that analyzes repositories, plans implementations, modifies code, executes tests, debugs failures, and produces verified Git commits using a multi-agent execution loop — Python, FastAPI, PostgreSQL, Redis, Docker sandboxing, Claude Opus 5 with structured outputs and prompt caching, an open-source model fallback via vLLM, and MCP interoperability. Resolved X % of SWE-bench Lite at $Y per task with a bounded debug loop and checkpointed, resumable runs.

Fill in X and Y from §14. Interviewers will ask about the transition function, the sandbox, and the debug budget — all three are yours to explain line by line.

## Two shapes of interrupt

A run can be waiting on a human in two different ways, and they are not the same state.

**An open question** happens *between* nodes. The Planner produced open questions, or an
escalation needs a decision, and nothing is half-finished. The run's `phase` is literally
`AWAITING_INPUT`, and `transition` sends it to `resume_phase` when the answer arrives.

**A tool approval** happens *inside* an agent's tool loop, with a turn on the stack and a
tool call waiting for a result. There is no phase for that, because the run has not
finished `CODE` or `DEBUG` — so the run's **status** becomes `awaiting_input` while its
`phase` stays where it was. `GET /runs/{id}` shows both fields, which is how a viewer
tells the two apart.

The practical consequences:

- An approval is answered with `POST /runs/{id}/approve` or `/reject`, quoting the
  `tool_call_id` from the `awaiting_input` event. An open question is answered with
  `/answer` and no id.
- The id of the call being waited on lives in Redis (`run:{id}:pending`), because the API
  process validates a decision against it and never sees `RunState`.
- Either way, the seconds spent parked are added to `waiting_s` and subtracted from the
  wall-clock budget. A human's thinking time is not the agent's.
- An unattended run auto-rejects approvals rather than hanging, and tells the model why,
  so it can work around the refusal.
