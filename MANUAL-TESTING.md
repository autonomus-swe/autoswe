# Manual testing tour — Phase 0

Phase 1 adds the single-agent loop on top of Phase 0's foundations. What exists today,
and what this tour exercises:

| # | Capability | Where |
|---|-----------|-------|
| 1 | CLI with fail-fast configuration | `cli/`, `core/settings.py` |
| 2 | Strict contracts for LLM output + task-graph logic | `contracts/` |
| 3 | Postgres schema, migrations, repository functions, cost roll-up | `storage/` |
| 4 | Redis bus: event stream, human inbox, locks, rate limit, cancel | `storage/redis.py` |
| 5 | Structured logging with secret redaction | `observability/logging.py` |
| 6 | OpenTelemetry spans | `observability/tracing.py` |
| 7 | Migration up/down | `alembic` |
| 8 | Docker sandbox: non-root, read-only, no network | `sandbox/` |
| 9 | Tool layer: bash, editor, run_tests, git | `tools/` |
| 10 | LLM gateway over any OpenAI-compatible endpoint | `gateway/` |
| 11 | Orchestrator, control plane and CLI | `orchestrator/`, `api/`, `cli/` |
| 12 | Automated suite | `tests/` |

Every command below was run and its output checked on 2026-09-07.

## 0. Setup (once per terminal)

```bash
cd ~/Desktop/proj/phase_0
source .venv/bin/activate      # or prefix every command with: uv run
make up && make migrate        # waits for healthy Postgres + Redis, then alembic upgrade head
```

`.env` is read from the current directory, so stay in `phase_0/` for the Python snippets.
Ports come from `.env` (`POSTGRES_PORT=5432`, `REDIS_PORT=6380`).

## 1. CLI and fail-fast settings

```bash
autoswe --help
autoswe version                # 0.0.1
autoswe config                 # masked view: postgres:***@..., "1 key(s)", anthropic_api_key unset
```

Prove that a bad or missing setting stops the process with exit code 2 and names the variable:

```bash
DATABASE_URL=mysql://nope autoswe config; echo "exit code: $?"
# FATAL: missing or invalid settings: DATABASE_URL
# exit code: 2

(cd /tmp && autoswe config); echo "exit code: $?"      # no .env there
# FATAL: missing or invalid settings: DATABASE_URL, REDIS_URL, API_KEYS
# exit code: 2
```

## 2. Contracts: what the LLM must produce, and the task graph

```bash
python - <<'EOF'
from pydantic import ValidationError
from contracts import Budget, TaskGraph, TaskGraphSpec, Usage

# 1. The planner LLM must produce exactly this shape: strict schema, no extra keys, <= 8 tasks
schema = TaskGraphSpec.model_json_schema()
print("additionalProperties:", schema["additionalProperties"], "| max tasks:", schema["properties"]["tasks"]["maxItems"])

# 2. A valid planner output parses into a runtime TaskGraph
raw = {"tasks": [
    {"id": "t1", "title": "Add model", "description": "Create the User model", "depends_on": [],
     "files": ["app/models.py"], "acceptance_criteria": ["User model exists"], "test_selector": "tests/test_models.py"},
    {"id": "t2", "title": "Add endpoint", "description": "POST /users", "depends_on": ["t1"],
     "files": ["app/api.py"], "acceptance_criteria": ["POST /users returns 201"], "test_selector": "tests/test_api.py"},
]}
graph = TaskGraph.from_spec(TaskGraphSpec.model_validate(raw))
print("dag problems:", graph.validate_dag())
print("next ready:", graph.next_ready().id)
graph.by_id("t1").status = "done"
print("after t1 done, next ready:", graph.next_ready().id)

# 3. A hallucinated field is rejected, not silently accepted
try:
    TaskGraphSpec.model_validate({"tasks": [dict(raw["tasks"][0], priority="high")]})
except ValidationError as e:
    print("rejected:", e.errors()[0]["type"], "at", e.errors()[0]["loc"])

# 4. Dependency cycles are reported in plain words
raw["tasks"][0]["depends_on"] = ["t2"]
print("cycle:", TaskGraph.from_spec(TaskGraphSpec.model_validate(raw)).validate_dag())

# 5. Usage arithmetic used for budget enforcement
u = Usage(input_tokens=1000, output_tokens=200, cost_usd=0.012).add(Usage(input_tokens=500, output_tokens=100, cost_usd=0.008))
print("usage:", u.total_tokens, "tokens,", f"${u.cost_usd:.3f}", "| default budget max_usd:", Budget().max_usd)
EOF
```

Expected:

```
additionalProperties: False | max tasks: 8
dag problems: []
next ready: t1
after t1 done, next ready: t2
rejected: extra_forbidden at ('tasks', 0, 'priority')
cycle: ['cycle: t1 -> t2 -> t1']
usage: 1800 tokens, $0.020 | default budget max_usd: 10.0
```

Try it yourself: change `"depends_on": ["t1"]` to `["t9"]` and rerun to see the unknown-task message.
Other schemas to inspect the same way: `TestReport`, `DebugHypothesis`, `ReviewReport`, `SecurityReport`,
`PullRequestDescription` (all in `contracts/`, all `additionalProperties: false`).

## 3. Postgres: schema, run lifecycle, cost roll-up

Look at the schema first (`psql` is not installed on the host, so go through the container):

```bash
docker compose exec postgres psql -U postgres -d autoswe -c '\dt' -c '\d runs' -c '\dx vector'
```

Now simulate what a run will write, using only the repository functions:

```bash
python - <<'EOF'
import asyncio
from contracts import Budget, TaskGraph, TaskGraphSpec, Usage
from storage import repo
from storage.db import dispose_engine, session

async def main():
    async with session() as s:  # commits at the end of the block
        run_id = await repo.create_run(s, repo_url="https://github.com/acme/demo", base_branch="main",
                                       goal="Add a /health endpoint", budget=Budget(max_usd=5))
        await repo.mark_run_started(s, run_id)
        await repo.set_run_phase(s, run_id, "plan", "running")
        graph = TaskGraph.from_spec(TaskGraphSpec.model_validate({"tasks": [
            {"id": "t1", "title": "Add route", "description": "GET /health returns 200", "depends_on": [],
             "files": ["app/api.py"], "acceptance_criteria": ["200 OK"], "test_selector": "tests/test_health.py"}]}))
        await repo.upsert_tasks(s, run_id, graph)
        await repo.insert_event(s, run_id, "phase_changed", {"phase": "code"})
        step = await repo.start_step(s, run_id=run_id, task_id="t1", agent="coder", phase="code", input={"task": "t1"})
        await repo.insert_llm_call(s, step_id=step, provider="anthropic", model="claude-opus-5", effort="high",
                                   usage=Usage(input_tokens=1200, output_tokens=300, cache_read_tokens=900, cost_usd=0.0150),
                                   latency_ms=850, stop_reason="tool_use")
        await repo.insert_tool_call(s, step_id=step, name="bash", input={"command": "pytest -q"},
                                    output_preview="1 passed", exit_code=0, duration_ms=340)
        await repo.insert_llm_call(s, step_id=step, provider="anthropic", model="claude-opus-5", effort="high",
                                   usage=Usage(input_tokens=400, output_tokens=120, cost_usd=0.0060),
                                   latency_ms=600, stop_reason="end_turn")
        cost = await repo.run_cost(s, run_id)  # summed from llm_calls: the budget source of truth
        await repo.finish_step(s, step, output={"summary": "route added"}, error=None, usage=cost)
        await repo.save_checkpoint(s, run_id, seq=1, phase="code", state={"graph": graph.model_dump(mode="json")})
        await repo.save_artifact(s, run_id, "test_report", None, {"passed": True, "total": 1})
        await repo.set_run_cost(s, run_id, cost.cost_usd)
        await repo.finish_run(s, run_id, status="done", pr_url="https://github.com/acme/demo/pull/1")

    async with session() as s:  # read it back in a fresh session
        row = await repo.get_run(s, run_id)
        print("run:", row.id, "| status:", row.status, "| branch:", row.work_branch, "| cost_usd:", row.cost_usd)
        print("tasks:", [(t.id, t.status) for t in await repo.list_tasks(s, run_id)])
        print("cost summed from llm_calls:", cost.model_dump())
        print("latest checkpoint seq:", (await repo.latest_checkpoint(s, run_id))[0])
        print("events:", [e.type for e in await repo.list_events(s, run_id)])
        print("latest test_report artifact:", (await repo.latest_artifact(s, run_id, "test_report")).content)
    await dispose_engine()

asyncio.run(main())
EOF
```

Expected (ids differ): status `done`, branch `agent/<run-id>`, `cost_usd: 0.0210`
(= 0.0150 + 0.0060, summed from the two `llm_calls` rows, not stored by hand), checkpoint seq 1,
events `['phase_changed']`, artifact `{'total': 1, 'passed': True}`.

Inspect the rows with SQL, then delete the run and watch the cascade remove every child row:

```bash
docker compose exec postgres psql -U postgres -d autoswe \
  -c "select status, phase, cost_usd, pr_url from runs" \
  -c "select model, input_tokens, output_tokens, cost_usd, stop_reason from llm_calls" \
  -c "select seq, kind, content from artifacts"

docker compose exec postgres psql -U postgres -d autoswe \
  -c "delete from runs where goal='Add a /health endpoint'" \
  -c "select count(*) as steps_left from steps"      # 0
```

## 4. Redis bus

Event stream, lock, token bucket, cancel flag in one go:

```bash
python - <<'EOF'
import asyncio, uuid
from core.settings import get_settings
from storage.redis import RedisBus

async def main():
    bus = RedisBus(get_settings().redis_url)
    run_id = uuid.uuid4()
    print("ping:", await bus.ping())

    # Event stream: append-only per run, replayable from any cursor (this is what the SSE endpoint will read)
    for phase in ("setup", "analyze", "plan"):
        await bus.emit(run_id, "phase_changed", {"phase": phase})
    events = await bus.read_events(run_id, "0-0", block_ms=100)
    print("replayed all:", [e["payload"]["phase"] for _, e in events])
    print("after 2nd id:", [e["payload"]["phase"] for _, e in await bus.read_events(run_id, events[1][0], block_ms=100)])

    # One run per repo+branch: exclusive lock, owner-checked renew/release
    key = "lock:repo:acme/demo:main"
    print("A acquires:", await bus.acquire_lock(key, "worker-A", ttl_s=30))
    print("B acquires:", await bus.acquire_lock(key, "worker-B", ttl_s=30))
    print("B releases A's lock:", await bus.release_lock(key, "worker-B"), "| owner still:", await bus.lock_owner(key))
    print("A releases:", await bus.release_lock(key, "worker-A"))

    # Token bucket per API key: burst of 3, then refill 1/s
    print("bucket (5 tries):", [await bus.take_token(f"rl:{run_id}", capacity=3, refill_per_s=1.0) for _ in range(5)])

    # Cancel flag polled by the worker between steps
    print("cancelled before:", await bus.is_cancelled(run_id))
    await bus.set_cancel(run_id)
    print("cancelled after:", await bus.is_cancelled(run_id))
    print("\nraw view:  redis-cli -p 6380 XRANGE run:%s:events - +" % run_id)
    await bus.close()

asyncio.run(main())
EOF
```

Expected: `replayed all: ['setup', 'analyze', 'plan']`, `after 2nd id: ['plan']`,
`A acquires: True`, `B acquires: False`, `B releases A's lock: False | owner still: worker-A`,
`bucket (5 tries): [True, True, True, False, False]`, cancelled `False` then `True`.
Paste the printed `redis-cli` line to see the raw stream entries, or `redis-cli -p 6380 KEYS 'run:*'`.

Human-in-the-loop inbox, two terminals. Terminal 1 (the "agent" blocks waiting for an answer):

```bash
python - <<'EOF'
import asyncio
from core.settings import get_settings
from storage.redis import RedisBus
async def main():
    bus = RedisBus(get_settings().redis_url)
    print("agent waiting up to 60s for a human answer on run demo-1 ...")
    print("got:", await bus.pop_inbox("demo-1", timeout_s=60))
    await bus.close()
asyncio.run(main())
EOF
```

Terminal 2 (the "human" answers; the inbox is a plain Redis list of JSON, so redis-cli works):

```bash
redis-cli -p 6380 RPUSH run:demo-1:inbox '{"type":"answer","text":"use JWT"}'
```

Terminal 1 prints `got: {'type': 'answer', 'text': 'use JWT'}` immediately.

## 5. Logging with secret redaction

```bash
python - <<'EOF'
from observability.logging import bind_run, configure_logging, get_logger
configure_logging("INFO")
log = get_logger("demo")
bind_run("11111111-2222-3333-4444-555555555555", task_id="t1")  # every line below carries run_id/task_id
log.info("provider_call", model="claude-opus-5", input_tokens=1200)
log.info("config_loaded", anthropic_api_key="sk-ant-api03-SHOULD-NOT-APPEAR", database_url="postgresql+asyncpg://postgres:dev@localhost/autoswe")
log.warning("tool_output", output="found ghp_abcdefghijklmnop1234 and postgresql://u:pw@db/x in file")
log.debug("hidden_below_level")
EOF
```

Expected: three JSON lines, each with `run_id` and `task_id`; the key and DSN become `[redacted]`
by field name, the GitHub token and `u:pw` inside free text become `[redacted]` by pattern,
and the debug line is filtered out. Rerun with `configure_logging("DEBUG", json=False)` for
coloured console output and the debug line.

## 6. Tracing

```bash
python - <<'EOF'
from opentelemetry.sdk.trace.export import ConsoleSpanExporter
from observability.tracing import configure_tracing, trace_span
configure_tracing("autoswe-demo", exporter=ConsoleSpanExporter())
with trace_span("run", run_id="demo-run"):
    with trace_span("agent.coder", task_id="t1", attempt=1):
        pass  # the inner span's parent_id equals the outer span's span_id
EOF
```

Expected: two JSON spans printed inner-first; `agent.coder` has `parent_id` equal to the
`span_id` of `run`, and the attributes you passed appear under `attributes`.

## 7. Migrations up and down

This drops and recreates every table, so run it only on a throwaway dev database:

```bash
alembic current                      # 0001 (head)
alembic history
alembic downgrade base
docker compose exec postgres psql -U postgres -d autoswe -c '\dt'     # no relations
alembic upgrade head
docker compose exec postgres psql -U postgres -d autoswe -c '\dt'     # 8 tables + alembic_version
```

## 8. Automated suite

```bash
make check          # ruff, ruff format, mypy --strict, 48 unit tests
make test-int       # 15 integration tests on throwaway Postgres + Redis containers
pytest -m unit -v   # read the test names as a list of guaranteed behaviours
```

## 9. Phase 1: the sandbox by hand

Build the image once, then poke at the container the agent gets:

```bash
make sandbox-image
python - <<'EOF'
import asyncio, os, uuid
from pathlib import Path
from sandbox.docker import DockerSandbox

ws = Path.home() / ".autoswe" / "tmp" / "manual"; ws.mkdir(parents=True, exist_ok=True)

async def main():
    box = DockerSandbox(uuid.uuid4(), ws, image="agent-sandbox:python-3.12",
                        network="agent-install", user=f"{os.getuid()}:{os.getgid()}")
    await box.start()
    print("whoami           ", (await box.exec("id -u")).stdout.strip())
    print("capabilities     ", (await box.exec("grep CapEff /proc/self/status")).stdout.strip())
    print("write /etc       ", (await box.exec("touch /etc/x")).exit_code, "(non-zero = read-only rootfs)")
    print("write /workspace ", (await box.exec("touch /workspace/ok")).exit_code)
    print("no host secrets  ", not any(k in (await box.exec("env")).stdout for k in ("ANTHROPIC","GITHUB","DATABASE_URL","LLM_API_KEY")))
    print("network before   ", await box.has_network())
    await box.disconnect_network()
    print("network after    ", await box.has_network(), "| dns:", (await box.exec("getent hosts pypi.org")).exit_code)
    print("timeout kills    ", (await box.exec("sleep 30", timeout_s=2)).timed_out)
    await box.stop()

asyncio.run(main())
EOF
```

## 10. Phase 1: the whole loop, no API key, no money

This is the strongest single check. It clones a real repository, starts the sandbox, lets
a scripted coder edit files through the tools, runs the tests, commits, pushes the branch
and opens a stub pull request, then asserts the database recorded every action in order.

```bash
uv run pytest tests/integration/test_full_run.py -v
```

## 11. Phase 1: a real model through OpenRouter

Get a key at <https://openrouter.ai/keys>, put it in `.env` as `LLM_API_KEY`, then:

```bash
uv run pytest -m e2e tests/e2e/test_m1_local.py -v -s
```

It runs the same loop against a real model and prints the tool-call count and the cost.
`LLM_MODEL` selects the model; the default is a free one. This test skips itself when
`LLM_API_KEY` is empty.

## 12. Phase 1: the control plane

Two terminals:

```bash
make api        # terminal 1: uvicorn on 127.0.0.1:8000
make worker     # terminal 2: the arq worker
```

Then:

```bash
curl -s localhost:8000/healthz | python3 -m json.tool
curl -s -X POST localhost:8000/runs -H 'X-API-Key: dev-key-change-me' \
  -H 'content-type: application/json' \
  -d '{"repo_url":"https://github.com/you/autoswe-fixture-python","goal":"Implement subtract(a, b) and slugify(text) in fixture/ops.py so that tests/test_ops.py passes. Do not change the tests."}'

export AUTOSWE_API_KEY=dev-key-change-me
uv run autoswe run --repo https://github.com/you/autoswe-fixture-python --goal "..."
uv run autoswe status <run-id>
```

Authentication and rate limiting, without a worker:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/runs \
  -H 'content-type: application/json' -d '{}'                      # 401
for i in $(seq 6); do curl -s -o /dev/null -w '%{http_code} ' \
  -H 'X-API-Key: dev-key-change-me' localhost:8000/runs/00000000-0000-0000-0000-000000000000; done
echo                                                                # 404 x5 then 429
```

Read the audit trail afterwards:

```bash
docker compose exec postgres psql -U postgres -d autoswe \
  -c "select phase, status, cost_usd, pr_url from runs order by created_at desc limit 5" \
  -c "select tc.seq, tc.name, tc.exit_code, tc.duration_ms from tool_calls tc join steps s on s.id = tc.step_id order by tc.seq" \
  -c "select model, input_tokens, output_tokens, cost_usd, stop_reason from llm_calls order by seq"
```

## Teardown

```bash
make down           # stops containers and deletes the pgdata volume
```
