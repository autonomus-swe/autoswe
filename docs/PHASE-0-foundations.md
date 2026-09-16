# Phase 0 — Foundations

> Outcome: a repository where every later phase drops into place. Tooling, settings that fail fast, the PostgreSQL schema, the Pydantic contracts, Redis helpers, structured logging, docker-compose, and CI. Nothing calls an LLM yet.

Suggested duration: 2–3 days.

---

## 0. Where this phase sits

**Prerequisites**
- A GitHub account with a fork-based workflow set up (personal fork of the project repo; `origin` = fork).
- Docker Engine and Docker Compose installed locally (`docker run hello-world` works without sudo).
- `uv` installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- Python 3.12 available to `uv` (`uv python install 3.12`).

**Delivers**
- `pyproject.toml`, `uv.lock`, `ruff`/`mypy`/`pytest` configuration, `Makefile`, `.pre-commit-config.yaml`, `.env.example`, `.gitignore`.
- `core/settings.py` (pydantic-settings, fail-fast), `core/errors.py`.
- `contracts/` — every schema from README §6 plus the runtime wrappers.
- `storage/models.py`, `storage/db.py`, `storage/migrations/` (Alembic, async), `storage/repo.py` (thin query functions), `storage/redis.py`.
- `observability/logging.py` (structlog JSON with `run_id`/`task_id`/`step_id` binding), `observability/tracing.py` (OpenTelemetry tracer that is a no-op until Phase 5 configures an exporter).
- `docker-compose.yml` with `postgres` (pgvector image) and `redis`.
- `tests/` scaffolding with testcontainers fixtures.
- `.github/workflows/ci.yml`.

**Deliberately deferred**
- Anything that talks to Anthropic, Docker-from-Python, or GitHub (Phase 1).
- `repo_symbols` and `repo_embeddings` tables (Phase 5; they get their own migration).
- OpenTelemetry exporters, Langfuse, Prometheus (Phase 5).

---

## 1. Exit criteria

- [x] `uv sync` on a clean clone succeeds; `make check` (ruff + mypy + unit tests) is green.
- [x] `make up && make migrate` creates all eight core tables in a fresh Postgres; `alembic downgrade base` removes them.
- [x] Starting any entrypoint with a missing required variable exits with code 2 and prints the variable name (test exists).
- [x] Every LLM-facing contract produces a JSON schema with `additionalProperties: false` (test exists).
- [x] `make test-int` runs Postgres and Redis integration tests via testcontainers and passes.
- [x] CI runs lint, type-check, unit tests, and integration tests on every push to the fork; gitleaks step is green.
- [x] `CHANGELOG.md` created with a `0.0.1` entry. Tag `v0.0.1`.

---

## 2. Architecture slice

```
              ┌──────────────┐        ┌──────────────┐
              │  PostgreSQL  │        │    Redis     │
              │  8 tables    │        │  helpers     │
              └──────▲───────┘        └──────▲───────┘
                     │ storage/db.py         │ storage/redis.py
   ┌─────────────────┴───────────────────────┴───────────────┐
   │  core/settings.py   contracts/*   observability/logging  │
   └──────────────────────────────────────────────────────────┘
```

Everything above is imported by every later component; nothing here imports from `api/`, `orchestrator/`, `agents/`, or `tools/`. Keep that direction — it is what makes the contracts testable without infrastructure.

---

## 3. Work breakdown

### Step 0.1 — Repository and toolchain

**Files:** `pyproject.toml`, `Makefile`, `.pre-commit-config.yaml`, `.gitignore`, `.env.example`, `CHANGELOG.md`, `README.md` (already present).

`pyproject.toml` (the parts that matter):

```toml
[project]
name = "autoswe"
version = "0.0.1"
requires-python = ">=3.12"
dependencies = [
  "pydantic>=2.9",
  "pydantic-settings>=2.5",
  "sqlalchemy[asyncio]>=2.0",
  "asyncpg>=0.29",
  "alembic>=1.13",
  "redis>=5.0",
  "structlog>=24.1",
  "opentelemetry-api>=1.27",
  "opentelemetry-sdk>=1.27",
  "orjson>=3.10",
]

[project.optional-dependencies]
api    = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "sse-starlette>=2.1"]
worker = ["arq>=0.26", "docker>=7.1", "GitPython>=3.1", "PyGithub>=2.4", "anthropic>=0.116"]
dev    = ["pytest>=8.3", "pytest-asyncio>=0.24", "pytest-cov", "testcontainers[postgres,redis]>=4.8",
          "ruff>=0.6", "mypy>=1.11", "types-redis", "pre-commit", "httpx", "typer"]

[project.scripts]
autoswe = "cli.main:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["api", "orchestrator", "agents", "tools", "sandbox", "repo", "gateway",
            "contracts", "storage", "mcp_bridge", "observability", "cli", "evals", "core"]

[tool.ruff]
line-length = 100
target-version = "py312"
[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "S", "ASYNC", "RUF"]
ignore = ["S101"]           # asserts in tests
[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S", "B"]

[tool.mypy]
python_version = "3.12"
plugins = ["pydantic.mypy"]
warn_unused_ignores = true
[[tool.mypy.overrides]]
module = ["contracts.*", "orchestrator.*", "tools.*", "gateway.*"]
strict = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
  "unit: no I/O",
  "integration: needs Postgres/Redis/Docker via testcontainers",
  "e2e: needs real API keys; never in CI by default",
]
addopts = "-q -m 'not e2e'"
```

Create the package directories now, each with an empty `__init__.py`, so imports resolve from day one: `api orchestrator agents tools sandbox repo gateway contracts storage mcp_bridge observability cli evals core`. Also `agents/prompts/`, `sandbox/images/`, `storage/migrations/`, `tests/unit`, `tests/integration`, `tests/e2e`.

`Makefile`:

```make
.PHONY: up down migrate lint type test test-int check sandbox-image
up:         ; docker compose up -d --wait postgres redis
down:       ; docker compose down -v
migrate:    ; uv run alembic upgrade head
lint:       ; uv run ruff check . && uv run ruff format --check .
type:       ; uv run mypy .
test:       ; uv run pytest -m unit
test-int:   ; uv run pytest -m integration
check: lint type test
sandbox-image: ; docker build -t agent-sandbox:python-3.12 sandbox/images/python
```

`.pre-commit-config.yaml`: ruff (check + format), mypy, and `gitleaks` (`repo: https://github.com/gitleaks/gitleaks`, hook `gitleaks`). Run `pre-commit install`.

`.env.example`:

```
DATABASE_URL=postgresql+asyncpg://postgres:dev@localhost:5432/autoswe
REDIS_URL=redis://localhost:6379/0
API_KEYS=dev-key-change-me
# Phase 1+
ANTHROPIC_API_KEY=
GITHUB_TOKEN=
WORKTREES_DIR=/var/agent/worktrees
REPOS_DIR=/var/agent/repos
SANDBOX_IMAGE=agent-sandbox:python-3.12
```

`.gitignore` must include `.env`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.adlc/session/`, `evals/results/`.

**Verify:** `uv sync --all-extras && make lint && make type` succeed on the empty packages.

---

### Step 0.2 — Settings that fail fast

**Files:** `core/settings.py`, `core/errors.py`, `tests/unit/test_settings.py`.

```python
# core/settings.py
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(pattern=r"^postgresql\+asyncpg://")
    redis_url: str = Field(pattern=r"^redis(s)?://")
    api_keys_raw: str = Field(alias="API_KEYS", min_length=8)

    # Phase 1+ (optional until the worker needs them; the worker calls require_worker())
    anthropic_api_key: SecretStr | None = None
    github_token: SecretStr | None = None
    worktrees_dir: Path = Path("/var/agent/worktrees")
    repos_dir: Path = Path("/var/agent/repos")
    sandbox_image: str = "agent-sandbox:python-3.12"

    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    @property
    def api_keys(self) -> frozenset[str]:
        return frozenset(k.strip() for k in self.api_keys_raw.split(",") if k.strip())

    def require_worker(self) -> None:
        """Called by the worker entrypoint only; the API never needs these."""
        missing = [n for n in ("anthropic_api_key", "github_token") if getattr(self, n) is None]
        if missing:
            _die(missing)


def _die(missing: list[str]) -> None:
    names = ", ".join(m.upper() for m in missing)
    print(f"FATAL: missing or invalid settings: {names}", file=sys.stderr)
    raise SystemExit(2)


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as e:
        _die([".".join(str(p) for p in err["loc"]) for err in e.errors()])
        raise  # unreachable; keeps mypy happy
```

Rules baked in:
- Secrets are `SecretStr` so they never appear in `repr()` or logs.
- The API process never loads `ANTHROPIC_API_KEY` or `GITHUB_TOKEN`; only the worker calls `require_worker()`. This is the first step toward "secrets live only in the worker" (README §4.5).
- `get_settings()` is cached; tests clear the cache with `get_settings.cache_clear()` after monkeypatching env.

`core/errors.py`: a small hierarchy used everywhere later — `AutosweError`, `ConfigError`, `PolicyViolation` (tool denied), `SandboxError`, `BudgetExceeded`, `ProviderError`.

**Tests (`tests/unit/test_settings.py`):**
- Missing `DATABASE_URL` → `SystemExit` with code 2 and stderr containing `DATABASE_URL`.
- `API_KEYS="a,b, c"` → `api_keys == {"a","b","c"}`.
- `require_worker()` without `GITHUB_TOKEN` exits naming it.

---

### Step 0.3 — Contracts

**Files:** `contracts/__init__.py` (re-exports), `contracts/common.py`, `contracts/repo.py`, `contracts/plan.py`, `contracts/testing.py`, `contracts/debug.py`, `contracts/review.py`, `contracts/security.py`, `contracts/pr.py`, `contracts/task_result.py`, `contracts/budget.py`, `contracts/events.py`, `contracts/tools.py`, `tests/unit/test_contracts.py`.

Two base classes set the rules:

```python
# contracts/common.py
from typing import ClassVar
from pydantic import BaseModel, ConfigDict

class LLMModel(BaseModel):
    """Schema the model must produce. Strict: unknown keys rejected, schema has additionalProperties=false."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    __test__: ClassVar[bool] = False   # stops pytest collecting classes named Test*

class StateModel(BaseModel):
    """Runtime state. May carry defaults, methods, and fields the model never sees."""
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    __test__: ClassVar[bool] = False
```

The LLM-facing models, verbatim from README §6 with the two-model split applied:

```python
# contracts/repo.py
class RepoProfile(LLMModel):
    languages: list[str]
    framework: str | None
    package_manager: str
    test_command: str
    lint_command: str | None
    conventions: list[str]
    entry_points: list[str]

# contracts/plan.py
class ImplementationPlan(LLMModel):
    approach: str
    affected_files: list[str]
    new_files: list[str]
    risks: list[str]
    test_strategy: str
    open_questions: list[str]

class TaskSpec(LLMModel):                       # what the Decomposer emits
    id: str
    title: str
    description: str
    depends_on: list[str]
    files: list[str]
    acceptance_criteria: list[str]
    test_selector: str

class TaskGraphSpec(LLMModel):
    tasks: list[TaskSpec]

TaskStatus = Literal["pending", "in_progress", "done", "failed"]

class Task(StateModel):                          # runtime wrapper
    spec: TaskSpec
    status: TaskStatus = "pending"
    attempts: int = 0
    replanned: bool = False
    @property
    def id(self) -> str: return self.spec.id

class TaskGraph(StateModel):
    tasks: list[Task]
    def by_id(self, task_id: str) -> Task: ...
    def next_ready(self) -> Task | None:
        """First pending task whose depends_on are all done, in list order (the decomposer emits topological order)."""
    def validate_dag(self) -> list[str]:
        """Returns human-readable problems: unknown dependency ids, duplicate ids, cycles. Empty list = valid."""
    @classmethod
    def from_spec(cls, spec: TaskGraphSpec) -> "TaskGraph": ...
```

```python
# contracts/testing.py
class Frame(LLMModel):
    file: str; line: int; function: str; code: str
    in_repo: bool

FailureKind = Literal["assertion", "exception", "import", "environment", "timeout"]

class TestFailure(LLMModel):
    test_id: str
    kind: FailureKind
    message: str
    frames: list[Frame]
    signature: str

class TestReport(LLMModel):
    passed: bool
    total: int; failed: int; errors: int; skipped: int
    failures: list[TestFailure]
    duration_s: float
    command: str
    truncated_output: str          # last ~4 KB of raw output, for humans only

# contracts/debug.py
class DebugHypothesis(LLMModel):
    failure_class: FailureKind | Literal["flaky"]
    root_cause: str
    plan: str
    confidence: float = Field(ge=0, le=1)

# contracts/task_result.py
class TaskResult(LLMModel):
    summary: str
    files_touched: list[str]
    how_to_test: str
    notes_for_reviewer: list[str]

# contracts/review.py
Severity = Literal["blocking", "major", "minor", "nit"]
class ReviewFinding(LLMModel):
    file: str; line: int
    severity: Severity
    category: str
    summary: str
    failure_scenario: str
class ReviewCandidates(LLMModel):                 # pre-pass output
    findings: list[ReviewFinding]
class ReviewReport(LLMModel):
    findings: list[ReviewFinding]
    blocking: bool

# contracts/security.py
class SecurityFinding(LLMModel):
    tool: str; rule: str; file: str; line: int
    severity: Literal["critical", "high", "medium", "low", "info"]
    message: str
    verified_by_llm: bool
    false_positive: bool
    rationale: str
class SecurityReport(LLMModel):
    findings: list[SecurityFinding]
    critical: bool
    checklist: dict[str, bool]

# contracts/pr.py
class PullRequestDescription(LLMModel):
    title: str; summary: str
    changes: list[str]; testing: str
    known_issues: list[str]; rollback: str
```

Runtime-only models:

```python
# contracts/budget.py
class Budget(StateModel):
    max_debug_attempts: int = 3
    max_fix_rounds: int = 2
    wall_clock_s: int = 45 * 60
    max_usd: float = 10.0
    max_tokens: int | None = None
    warn_at_fraction: float = 0.9

class Usage(StateModel):
    input_tokens: int = 0; output_tokens: int = 0
    cache_read_tokens: int = 0; cache_write_tokens: int = 0
    cost_usd: float = 0.0
    wall_clock_s: float = 0.0
    def add(self, other: "Usage") -> "Usage": ...

# contracts/tools.py
class ExecResult(StateModel):
    exit_code: int; stdout: str; stderr: str
    duration_ms: int; truncated: bool; timed_out: bool

class ToolResult(StateModel):
    content: str                       # what the model sees
    is_error: bool = False
    artifact: dict | None = None       # structured payload for the harness (e.g. a TestReport)

# contracts/events.py
EventType = Literal["phase_changed","agent_started","agent_text","tool_call","tool_result",
                    "test_report","debug_hypothesis","review_report","security_report",
                    "awaiting_input","pr_opened","budget_warning","run_finished","log"]
class Event(StateModel):
    run_id: UUID; type: EventType; ts: datetime; payload: dict
```

Keep `dict[str, bool]` (`SecurityReport.checklist`) as the only mapping in an LLM-facing model. Structured-output schemas dislike open-ended maps; if the API rejects it in Phase 4, replace it with an explicit `SecurityChecklist(LLMModel)` of named booleans (the README §9 list). Write that class now, commented out, so the swap is a one-liner.

**Tests (`tests/unit/test_contracts.py`):**
- For every subclass of `LLMModel`: `model_json_schema()["additionalProperties"] is False` and no `$ref` cycles.
- `TaskGraph.from_spec` + `next_ready()` on a three-task chain returns them in order as statuses flip to `done`.
- `validate_dag()` reports a cycle (`a→b→a`) and an unknown dependency.
- Round-trip: every model `model_validate(model.model_dump())` equals itself.
- Pytest emits no `PytestCollectionWarning` for `TestReport`/`TestFailure` (run with `-W error::pytest.PytestCollectionWarning`).

---

### Step 0.4 — PostgreSQL schema, SQLAlchemy models, Alembic

**Files:** `storage/db.py`, `storage/models.py`, `storage/repo.py`, `storage/migrations/env.py`, `storage/migrations/versions/0001_core_tables.py`, `alembic.ini`, `tests/integration/test_storage.py`.

`storage/db.py`:

```python
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from core.settings import get_settings

_engine = None
def engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True, pool_size=10)
    return _engine

SessionLocal = lambda: async_sessionmaker(engine(), expire_on_commit=False)()

@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise
```

`storage/models.py` — SQLAlchemy 2.0 `Mapped[]` classes for README §7. Additive columns beyond the README DDL (all nullable or defaulted, so §7 stays true):

| Table | Extra columns | Reason |
|---|---|---|
| `runs` | `provider text default 'anthropic'`, `unattended bool default false`, `started_at`, `finished_at`, `error text`, `base_sha text` | resume, provider choice (Phase 6), audit |
| `tasks` | `replanned bool default false` | Phase 3 escalation |
| `steps` | `attempt int default 0`, `usage jsonb` | debug attempts, cost per step |
| `events` | index on `(run_id, id)` | SSE replay |
| `checkpoints` | `phase text` | quick inspection without unpacking JSONB |

JSONB columns use `sqlalchemy.dialects.postgresql.JSONB`. UUID columns use `Uuid(as_uuid=True)`. Timestamps are `DateTime(timezone=True)` with `server_default=func.now()`.

Alembic: `alembic init -t async storage/migrations`; point `env.py` at `storage.models.Base.metadata` and read the URL from `get_settings()`. Migration `0001_core_tables` creates `runs, tasks, steps, tool_calls, llm_calls, checkpoints, events, artifacts` and also runs `CREATE EXTENSION IF NOT EXISTS vector` (harmless now, needed in Phase 5).

`storage/repo.py` — thin, explicit functions; no ORM magic leaks upward:

```python
async def create_run(s, *, repo_url, base_branch, goal, budget: Budget, provider="anthropic") -> UUID
async def get_run(s, run_id) -> RunRow | None
async def set_run_phase(s, run_id, phase: str, status: str) -> None
async def finish_run(s, run_id, *, status, pr_url=None, error=None) -> None
async def upsert_tasks(s, run_id, graph: TaskGraph) -> None
async def start_step(s, *, run_id, task_id, agent, phase, input: dict, attempt=0) -> UUID
async def finish_step(s, step_id, *, output: dict | None, error: str | None, usage: Usage) -> None
async def insert_tool_call(s, *, step_id, name, input, output_preview, exit_code, duration_ms, approved_by=None) -> None
async def insert_llm_call(s, *, step_id, provider, model, effort, usage: Usage, latency_ms, stop_reason) -> None
async def save_checkpoint(s, run_id, seq: int, phase: str, state: dict) -> None
async def latest_checkpoint(s, run_id) -> tuple[int, dict] | None
async def insert_event(s, run_id, type: str, payload: dict) -> None
async def save_artifact(s, run_id, kind: str, path: str | None, content: dict) -> None
async def run_cost(s, run_id) -> Usage          # SUM over llm_calls; the budget source of truth
```

**Tests (`tests/integration/test_storage.py`, marker `integration`):** with a testcontainers Postgres, run `alembic upgrade head` programmatically, then: create run → start step → insert two llm_calls → `run_cost()` sums correctly → save two checkpoints → `latest_checkpoint()` returns seq 2 → `alembic downgrade base` leaves no tables.

---

### Step 0.5 — Redis helpers

**Files:** `storage/redis.py`, `tests/integration/test_redis.py`.

```python
class RedisBus:
    def __init__(self, url: str): self.r = redis.asyncio.from_url(url, decode_responses=True)

    # events (README §4.8) — append-only stream per run, trimmed to ~10k entries
    async def emit(self, run_id: UUID, type: str, payload: dict) -> str:
        return await self.r.xadd(f"run:{run_id}:events", {"type": type, "payload": orjson.dumps(payload)},
                                 maxlen=10_000, approximate=True)
    async def read_events(self, run_id, last_id: str = "0-0", block_ms: int = 15_000) -> list[tuple[str, dict]]:
        ...  # XREAD BLOCK; returns [(stream_id, {"type":..., "payload":...})]

    # human-in-the-loop inbox
    async def push_inbox(self, run_id, message: dict) -> None      # RPUSH run:{id}:inbox
    async def pop_inbox(self, run_id, timeout_s: int) -> dict | None   # BLPOP

    # one run per repo+branch
    async def acquire_lock(self, key: str, owner: str, ttl_s: int) -> bool   # SET NX PX
    async def renew_lock(self, key, owner, ttl_s) -> bool                    # Lua: only if owner matches
    async def release_lock(self, key, owner) -> None                          # Lua: only if owner matches

    # token bucket per API key (Phase 1 uses it on POST /runs)
    async def take_token(self, bucket: str, capacity: int, refill_per_s: float) -> bool   # Lua script

    # cancel flag (Phase 2)
    async def set_cancel(self, run_id) -> None
    async def is_cancelled(self, run_id) -> bool
```

The lock and token-bucket operations must be Lua scripts (`r.register_script`) so check-and-set is atomic. Write them now; Phase 1 and Phase 2 only call them.

**Tests:** emit three events, read from `0-0` gets three, read from the second ID gets one; `pop_inbox` with timeout returns `None` then returns the pushed message; lock is exclusive and only the owner can release; token bucket allows `capacity` calls then denies, allows again after refill.

---

### Step 0.6 — Logging and tracing skeleton

**Files:** `observability/logging.py`, `observability/tracing.py`, `tests/unit/test_logging.py`.

- `configure_logging(level, json=True)` sets up structlog with `merge_contextvars`, timestamps, log level, and JSON rendering. `bind_run(run_id, task_id=None, step_id=None)` binds contextvars so every later log line carries them.
- A processor `drop_sensitive` removes keys named `api_key`, `token`, `authorization`, `password`, and any value that looks like `sk-ant-` or `ghp_`. This is the README's "never log secrets" rule enforced in code, not by convention.
- `observability/tracing.py` exposes `trace_span(name, **attrs)` as a context manager over `opentelemetry.trace.get_tracer("autoswe")`. With no exporter configured it is a no-op; Phase 5 adds exporters without touching call sites.

**Tests:** a log call with `api_key="sk-ant-xxx"` in the event dict renders without that value; bound `run_id` appears in the JSON line.

---

### Step 0.7 — docker-compose and local bring-up

**Files:** `docker-compose.yml`, `docs/local-dev.md` (short).

Start with only what Phase 0 uses; later phases add services:

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment: { POSTGRES_PASSWORD: dev, POSTGRES_DB: autoswe }
    ports: ["5432:5432"]
    healthcheck: { test: ["CMD-SHELL", "pg_isready -U postgres"], interval: 5s, retries: 10 }
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    command: ["redis-server", "--save", "", "--appendonly", "no"]
volumes: { worktrees: {} }
```

**Verify:** `make up && make migrate && uv run python -c "import asyncio, storage.db as d; print(asyncio.run(d.engine().connect().__aenter__()))"` — or simpler, `make test-int`.

---

### Step 0.8 — Test scaffolding

**Files:** `tests/conftest.py`, `tests/integration/conftest.py`.

```python
# tests/integration/conftest.py
@pytest.fixture(scope="session")
def pg_url():
    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as pg:
        yield pg.get_connection_url()

@pytest.fixture(scope="session")
def redis_url():
    with RedisContainer("redis:7-alpine") as r:
        yield f"redis://{r.get_container_host_ip()}:{r.get_exposed_port(6379)}/0"

@pytest.fixture(autouse=True)
def _settings(monkeypatch, pg_url, redis_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("API_KEYS", "test-key-123456")
    get_settings.cache_clear()

@pytest.fixture
async def migrated_db(pg_url):
    # run alembic upgrade head once per session, truncate tables between tests
```

Unit tests never touch these fixtures; keep `tests/unit/conftest.py` free of containers so `make test` runs in seconds.

---

### Step 0.9 — CI

**File:** `.github/workflows/ci.yml`.

Jobs:
1. `lint-type` — `uv sync --all-extras`, `make lint`, `make type`.
2. `unit` — `make test` with coverage upload as artifact.
3. `integration` — needs Docker (GitHub's ubuntu runners have it); `make test-int`. Set `TESTCONTAINERS_RYUK_DISABLED=true` if Ryuk misbehaves on the runner.
4. `gitleaks` — `gitleaks/gitleaks-action@v2`.

Trigger on `push` and `pull_request`. Cache `~/.cache/uv`.

---

## 4. Testing plan

| Tier | What | Command | Runs in CI |
|---|---|---|---|
| unit | settings, contracts, DAG logic, logging redaction | `make test` | yes |
| integration | migrations up/down, repo functions, Redis helpers | `make test-int` | yes |
| e2e | none yet | — | — |

Coverage target for `contracts/` and `storage/repo.py`: 90 %.

---

## 5. Demo script

```bash
git clone git@github.com:<you>/autoswe.git && cd autoswe
uv sync --all-extras
cp .env.example .env
make up && make migrate
make check && make test-int
uv run python -c "from core.settings import get_settings; print(get_settings().api_keys)"
# now break it on purpose:
env REDIS_URL=redis://localhost:6379/0 API_KEYS=dev-key-change-me uv run python -c "from core.settings import load_settings; load_settings(None)"; echo "exit=$?"
# `env -u DATABASE_URL` does not demonstrate this: pydantic-settings reads .env from
# disk, so the variable returns and the process starts. env_file=None is the real test.
# → FATAL: missing or invalid settings: DATABASE_URL ; exit=2
```

---

## 6. Pitfalls

- **Pytest collecting `TestReport`.** Any class whose name starts with `Test` and is imported into a test module gets collected. The `__test__ = False` class variable on the contract base classes prevents it. Keep it.
- **Alembic async env.** Use the `-t async` template. The synchronous template will hang on `asyncpg`.
- **`extra="forbid"` on settings.** Do not do it. Your shell has hundreds of variables; pydantic-settings only reads declared fields, but `extra="forbid"` with an `.env` file that has a stray key will crash startup for the wrong reason. Use `extra="ignore"` for settings and `extra="forbid"` for contracts.
- **Structured-output schema limits.** Avoid `dict[str, Any]`, recursive models, `Union` of many models, and `Enum` classes in LLM-facing contracts. Use `Literal[...]` and flat lists. The test in Step 0.3 catches `$ref` cycles early.
- **uv and top-level packages.** If `import api` fails inside `uv run`, the hatch `packages` list is missing that package name. Every new top-level package must be added there.

---

## 7. Checklist before Phase 1

- [x] Exit criteria in §1 all ticked.
- [x] You can explain the two-model rule and why `TaskGraphSpec` and `TaskGraph` are different classes.
- [x] `run_cost()` is the only place that computes spend from `llm_calls`. Phase 3 budgets will depend on it.
- [x] Tag `v0.0.1`.
