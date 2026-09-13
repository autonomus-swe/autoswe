# Phase 2 — Plan and State (README milestone M2)

> Outcome: the run understands the repository before it edits it, breaks the goal into a task graph, checkpoints after every node, resumes after a crash, streams events live, and pauses for a human when the plan has open questions. Demo line: "Kill the worker mid-run; restart; it continues."

Suggested duration: 5–7 days.

---

## 0. Where this phase sits

**Prerequisites**
- Phase 1 exit checklist ticked (`v0.1.0`); the M1 e2e test passes.

**Delivers**
- `tools/fs.py` (`read_file`), `tools/search.py` (`search_code`)
- `repo/profile.py` (deterministic `RepoFacts`), `repo/repomap.py` (v1 tree-based map)
- `agents/analyzer.py`, `agents/planner.py`, `agents/decomposer.py` + prompts
- `orchestrator/state.py` (v2: the README `RunState`), `orchestrator/checkpoint.py`, `orchestrator/resume.py`
- `orchestrator/nodes.py`: `analyze_node`, `plan_node`, `decompose_node`, `awaiting_input_node`; multi-task `code_node`/`test_node`
- `orchestrator/transition.py` (v2)
- `orchestrator/events.py` (emit to Redis Stream + `events` table)
- `api/routes/events.py` (`GET /runs/{id}/events` SSE), `api/routes/control.py` (`POST /runs/{id}/answer`, `POST /runs/{id}/cancel`)
- `cli/main.py`: `watch`, `answer`, `cancel`
- Worker: retries with resume, lock renewal, cooperative cancel

**Deliberately deferred**
- `list_symbols`, tree-sitter, ranked repo map, semantic search (Phase 5). The v1 repo map is a file tree.
- Debugger and everything that happens when tests fail (Phase 3). A failing task still ends the run as `FAILED`.
- Review, security, PR Writer (Phase 4).
- Tool approvals (`requires_approval`, `approve`/`reject` endpoints) — Phase 3, together with `ask_user`.

---

## 1. Exit criteria

- [x] On the fixture repo, `ANALYZE` produces a `RepoProfile` with the right test command without any hard-coded default (test exists). — `tests/unit/test_profile.py`, including `test_empty_repo_detects_nothing_rather_than_guessing`.
- [x] `PLAN` and `DECOMPOSE` produce validated `ImplementationPlan` and `TaskGraph` objects stored in `steps.output` and `tasks`. — `test_full_run.py::test_the_plan_and_task_graph_land_where_phase_3_will_read_them`. Note the decomposer stores the runtime `TaskGraph` (per-task status included), not the bare `TaskGraphSpec`.
- [x] `checkpoints` has one row per node executed; `SIGKILL` the worker after `PLAN`, restart, and the run finishes with exactly one `planner` step (test exists). — `tests/integration/test_resume.py`. Real `SIGKILL` to a real child process; an in-process exception proves nothing because `runner.run` catches it, marks the run failed and tears the sandbox down.
- [x] `GET /runs/{id}/events` streams `phase_changed`, `agent_started`, `tool_call`, `tool_result`, `test_report`, `run_finished`; reconnecting with `Last-Event-ID` replays only what was missed (test exists). — `test_events_and_control.py` for replay and the cursor, `test_full_run.py` for the event set. One deviation: there is no separate `tool_result` event. A single `tool_call` event carries `is_error` and `duration_ms`, so the outcome is on the stream without doubling its volume.
- [x] A goal that forces open questions pauses the run in `AWAITING_INPUT`; `POST /answer` resumes it and the answer appears in the plan step's input (test exists). — `test_full_run.py::test_an_open_question_pauses_the_run_and_the_answer_is_on_the_record` covers the whole chain with the scripted provider: the run parks, the answer resumes it, and the second `planner` step's `input` carries it while the first does not. `test_events_and_control.py::test_answer_is_delivered_only_while_the_run_waits` covers the endpoint, including the 409 when the run is not waiting.
- [x] `POST /cancel` stops a running run within one node boundary or one tool call, whichever is first; the container is removed. — `test_full_run.py::test_cancel_stops_a_run_inside_a_tool_loop_and_removes_the_container`. The tool-call half needed implementing: `before_tool` raises `RunCancelled`, the runner records status `cancelled` and does not re-raise, so arq will not retry a job a human asked to stop. The worktree is kept on purpose — nothing was pushed, so the work is still recoverable.
- [ ] A goal that decomposes into three or more tasks completes with one commit per task. — **the loop itself is proven against a real model; the criterion is not ticked because no run has finished.** On 2026-09-13 with `poolside/laguna-s-2.1:free` the run went SETUP → ANALYZE → PLAN → DECOMPOSE → CODE → TEST with every agent working, and ended `failed` because the first task's tests did not pass. Phase 2 has no debug loop by design, so that ends the run. The goal now hands the agent the failing tests instead of asking it to write them, which is the Phase 1 fixture idiom and removes the model's test-writing from what this criterion measures. See "Provider note".
- [ ] Tag `v0.2.0`.

### Provider note (why an e2e box is unticked)

The e2e tests need a real model with tool calling. Neither free option covers a
multi-task coding run:

- **Hugging Face Inference Providers — cannot run this agent at all.** Not "out of quota
  today": a free account cannot make a single tool-calling request. The router advertises
  models at `$0/$0`, but that is a *rate*, not free access, and the gate is a pre-flight
  estimate against a small monthly credit pool. Measured on `Qwen/Qwen3.8-27B:ovhcloud`,
  the zero-rate provider, pinned:

  | request | result |
  |---|---|
  | tiny prompt, `max_tokens=400` | `200` |
  | tiny prompt, `max_tokens=4000` | `200` |
  | tiny prompt, `max_tokens=16000` | `402` |
  | tiny prompt **+ one tool definition** | `402` |
  | large prompt, `max_tokens=400` | `402` |

  One tool definition is enough to trip it, and every agent turn carries a tool set and a
  repo map. Pinning the zero-rate provider does not help, because the estimate charges the
  pool regardless. Do not be misled by `is_free` or a `$0` price in `GET /v1/models`;
  neither reflects whether a call will be served. Checked again on 2026-09-13 after the
  pool had partly replenished — small calls succeeded, every agent-shaped call did not.
- **GitHub Models** — retired. Both endpoints answer `410
  github_models_retirement_brownout` as of 2026-09-13.
- **OpenRouter free tier** — two separate limits, and they fail the same way:
  `free-models-per-min` at 20, and `free-models-per-day` at **50**. The per-minute one
  is now waited out (`gateway/openai_compat_provider.py`, verified absorbing 12 of them
  in one run). The daily 50 is the real wall: a multi-task run spends it, and the reset
  is hours away, which is not something a test can wait for — so the retry clamps and
  gives up rather than parking the suite. Roughly three runs a day. `$10` of credit
  raises it to 1000/day, which would be enough.

What this means in practice: OpenRouter is the only free option that works, so run
**one** e2e test on a fresh day's quota rather than the pair, before anything else spends
it. The pause test is the cheaper one — it only needs ANALYZE and PLAN.

**Which free model.** `openrouter/free` is an auto-router and cannot drive the Analyzer:
it explored for eleven of its twelve allowed turns and never submitted a profile. Probing
the free tool-capable models with one call each — a profiling task, an explore tool and a
submit tool — `poolside/laguna-s-2.1:free` called submit immediately where
`nvidia/nemotron-3-ultra-550b-a55b:free` and `cohere/north-mini-code:free` started
exploring. That probe costs four requests and is worth it before spending a whole run.

That first failure also exposed a real harness flaw rather than only a weak model: the
"you still have to submit" reminder shared the iteration budget, so it landed on the
final turn and the run died at `max_iterations` holding work it had already done.
Reminding with no headroom is the same as not reminding. A reminder now buys a turn
(`gateway/openai_compat_provider.py`), bounded at `max_iterations +
MISSING_SUBMIT_REMINDERS`.

**Why the goal hands over the tests.** `MULTI_TASK_GOAL` names five one-line functions and
the test files that import them, and `tests/e2e/test_m2.py` commits the failing
`tests/test_extra.py` before the run starts. The criterion is about the multi-task
CODE/TEST loop; asking the agent to write its own tests measures something else, and that
is exactly what ended the run that otherwise worked. It is the same fixture idiom Phase 1
uses: give the tests, make the agent implement, forbid touching them. The file is written
by the test rather than added to `tests/fixtures/fixture_repo`, because the TEST phase runs
the whole suite after each task and the scripted integration coder could not satisfy it.

The scripted-provider integration tests cover the same wiring without spend, so this is
an unverified-against-a-real-model gap rather than an untested one.

One finding worth keeping even though the run did not complete: given the goal
`"Add authentication."` with `unattended=False`, the Planner did **not** pause — it
planned and went straight to coding task `t2`. §6 warns about a planner that asks too
many questions; the opposite is also a failure, and this is the prompt to look at first
when the pause test next runs.

---

## 2. Architecture slice

```
SETUP ──► ANALYZE ──► PLAN ──► DECOMPOSE ──► ┌ CODE(t) ──► TEST(t) ┐ ──► PR ──► DONE
             │          │  open_questions?    └────── next task ────┘
             │          ▼
             │    AWAITING_INPUT ◄── POST /runs/{id}/answer
             ▼
        RepoFacts (deterministic) + RepoProfile (Analyzer) + repo map v1

   every node ──► checkpoint(RunState) ──► checkpoints table
   every node/tool ──► emit(event) ──► Redis Stream run:{id}:events ──► SSE ──► CLI watch
                                     └─► events table (durable replay)
```

---

## 3. Work breakdown

### Step 2.1 — Read-only tools

**Files:** `tools/fs.py`, `tools/search.py`, `tests/unit/test_fs_tools.py`.

- `read_file(path, start_line?, end_line?)` — read-only, parallel-safe. Host-side on the worktree after `confine()`. Returns numbered lines; default cap 400 lines per call with "showing 1–400 of 1 320; pass start_line to continue". Binary files → error. Records the view hash like the editor does, so a `str_replace` after `read_file` is not stale.
- `search_code(pattern, glob?, max_results=50, fixed_string=false, context=0)` — read-only, parallel-safe. Runs `rg --json --max-count … --glob … -e pattern` on the worktree host-side (add `ripgrep` to the worker image), parses the JSON lines into `path:line: text` entries, and truncates with a count of omitted matches. Never searches `.git`, `.venv`, `node_modules`, `.autoswe`.
- Register both; add `read_file` and `search_code` to the Coder's tool list.

**Tests:** ranges, cap message, path escape refusal, ripgrep glob filtering (fixture directory), JSON parsing of a saved `rg --json` output.

---

### Step 2.2 — Deterministic repository facts and repo map v1

**Files:** `repo/profile.py`, `repo/repomap.py`, `contracts/repo.py` (add `RepoFacts`), `tests/unit/test_profile.py`, `tests/unit/test_repomap.py`.

`RepoFacts` is a `StateModel` (never shown to the model as a schema, only rendered as text):

```python
class RepoFacts(StateModel):
    languages: list[str]                 # by file-extension share, ≥5 % or a manifest present
    package_manager: str | None          # uv | pip | poetry | npm | pnpm | yarn | go
    install_command: str | None
    test_command: str | None
    lint_command: str | None
    python_version: str | None
    file_count: int
    top_level: list[str]
    manifests: list[str]
    ci_test_lines: list[str]             # lines from .github/workflows containing pytest/npm test/go test
    readme_head: str                     # first 60 lines of README, fenced later
```

Detection order for `test_command` (first hit wins, and record which rule fired for debugging):
1. `.github/workflows/*.yml` run lines containing `pytest`, `npm test`, `pnpm test`, `go test`.
2. `pyproject.toml` → `[tool.pytest.ini_options]` present → `uv run --no-sync pytest -q`; `[tool.poetry]` → `poetry run pytest -q`.
3. `package.json` scripts.test → `npm test --silent`.
4. `go.mod` → `go test ./...`.
5. `Makefile` target `test` → `make test`.
6. Nothing → `None` (the Analyzer must decide; for an empty repo it proposes one).

Install command follows the package manager: `uv sync` (pyproject with uv lock or no lock), `uv venv && uv pip install -r requirements.txt`, `npm ci`, `go mod download`. `SETUP` now uses `RepoFacts.install_command` instead of Phase 1's hard-coded logic, and `RunState.test_command` is filled from `RepoProfile.test_command` after `ANALYZE`.

`repo/repomap.py` v1: `render_map(worktree, max_lines=150) -> str`. Walk the tree skipping `.git`, `.venv`, `node_modules`, `dist`, `build`, `__pycache__`, binaries and files over 1 MB. Score directories: manifests and `src/`/`app/`/package dirs first, then `tests/`, then docs; within a directory, sort by name. Render an indented tree with file sizes; when the budget is hit, collapse the rest of a directory into `… (+N files)`. This is enough for repos up to a few hundred files; Phase 5 replaces the scoring with symbols and centrality without changing the function signature.

**Tests:** fixture directories for pyproject+CI, package.json, go.mod, empty repo; map rendering respects `max_lines` and always includes manifests.

---

### Step 2.3 — Analyzer agent

**Files:** `agents/analyzer.py`, `agents/prompts/analyzer.md`, `orchestrator/nodes.py` (`analyze_node`), `tests/unit/test_analyzer.py`.

- Role `analyzer` (Sonnet 5, medium). Tools: `read_file`, `search_code`, `submit_profile` (schema `RepoProfile`). No mutating tool — `tools_for("analyzer")` asserts it.
- User message: rendered `RepoFacts`, the repo map, the README head inside the untrusted fence, and the goal (so conventions relevant to the goal get attention).
- Prompt rules: confirm or correct the detected test/lint commands by reading the config files; list conventions as short imperative sentences ("routers live in app/api/, one file per resource"); for an empty or near-empty repo, propose a standard layout for the detected or requested stack and say that it is proposed; cap at 8 tool calls (`max_iterations=12`) — this is a survey, not an audit.
- `analyze_node`: start step → run → `state.repo = ctx.submitted["profile"]`; `state.test_command = state.repo.test_command`; finish step; emit `agent_started`/`agent_finished` events.

**Tests:** fake provider returns a profile; node copies `test_command` into state and refuses a profile whose test command is empty.

---

### Step 2.4 — Planner agent and open questions

**Files:** `agents/planner.py`, `agents/prompts/planner.md`, `orchestrator/nodes.py` (`plan_node`), `tests/unit/test_planner.py`.

- Role `planner` (Opus 5, high). Tools: `read_file`, `search_code`, `submit_plan` (schema `ImplementationPlan`). Read-only.
- User message: goal, `RepoProfile`, repo map, and — on re-entry after `AWAITING_INPUT` — the Q&A list (`state.answers`).
- Prompt rules: the plan must name every file to change or create; `test_strategy` must use the repo's test command and name the test files; `risks` are specific ("migration renames a column used by …"); `open_questions` only when the goal is ambiguous in a way that changes the design — the prompt gives two examples of what is and is not blocking, otherwise the planner asks too much; never ask about things the plan can decide with a stated assumption.
- `plan_node`: run → `state.plan`; if `plan.open_questions` is non-empty and `state.unattended` is true, re-run the planner once with "no human is available; proceed with stated assumptions" and require `open_questions == []`.

**Tests:** plan with questions and `unattended=False` leaves `state.plan.open_questions` set (transition sends it to `AWAITING_INPUT`); with `unattended=True` the planner is invoked twice.

---

### Step 2.5 — Decomposer agent and task graph validation

**Files:** `agents/decomposer.py`, `agents/prompts/decomposer.md`, `orchestrator/nodes.py` (`decompose_node`), `tests/unit/test_decomposer.py`.

- Role `decompose` (Opus 5, high). No tools: `provider.parse(req, TaskGraphSpec)`.
- User message: goal, plan, profile, repo map.
- Prompt rules: 2–8 tasks; each task independently testable; `depends_on` only when a task cannot compile or run without the other; list `files` the task will touch; `acceptance_criteria` as checkable statements; `test_selector` names a test file or node id that exists or that this task creates; emit tasks in an order that respects dependencies; the first task creates any missing test scaffolding.
- `decompose_node`: parse → `TaskGraph.from_spec` → `validate_dag()`. On problems, re-run once with the problems appended ("fix these and return the full graph"); if still invalid, `state.error` and `FAILED`. Store tasks with `upsert_tasks`.

**Tests:** cycle → second call with the error text → valid; a graph with 9 tasks is rejected by the schema-level validator (`max_length=8` on `tasks`).

---

### Step 2.6 — `RunState` v2, checkpoints, resume, idempotent nodes

**Files:** `orchestrator/state.py`, `orchestrator/checkpoint.py`, `orchestrator/resume.py`, `orchestrator/runner.py`, `tests/unit/test_checkpoint.py`, `tests/integration/test_resume.py`.

`RunState` becomes the README §4.2 model (plus fields Phase 1 introduced):

```python
class RunState(StateModel):
    run_id: UUID; phase: Phase = Phase.SETUP
    goal: str; repo_url: str; base_branch: str; work_branch: str
    base_sha: str | None = None
    unattended: bool = False
    facts: RepoFacts | None = None
    repo: RepoProfile | None = None
    plan: ImplementationPlan | None = None
    answers: list[tuple[str, str]] = []
    tasks: TaskGraph | None = None
    current_task_id: str | None = None
    attempts: dict[str, int] = {}
    last_test_report: TestReport | None = None
    task_results: dict[str, TaskResult] = {}
    review: ReviewReport | None = None          # Phase 4
    security: SecurityReport | None = None      # Phase 4
    pr_url: str | None = None
    error: str | None = None
    budget: Budget = Budget()
    usage: Usage = Usage()
    seq: int = 0                                 # checkpoint sequence
    started_at: datetime | None = None
```

`checkpoint.py`: `save(state)` increments `state.seq` and writes `checkpoints(run_id, seq, phase, state.model_dump(mode="json"))`; `load_latest(run_id) -> RunState | None`. The runner calls `save` after every node and after every transition.

`resume.py`: `resume(run_id, deps) -> RunState`:
1. Load the latest checkpoint; if none, build the initial state from the `runs` row (a run that crashed inside `SETUP`).
2. Re-attach infrastructure: bare clone exists → reuse; worktree exists → reuse, else recreate from `base_sha`; container `run-<id>` exists and is running → reuse, else start a new one and re-run the install step; verify no network.
3. Re-acquire the repo lock (owner = worker id).
4. Continue `runner.run(state)` from `state.phase`.

Node idempotency, which resume depends on:

| Node | On re-run after a crash |
|---|---|
| `SETUP` | every sub-step checks for existing artifacts before creating |
| `ANALYZE`, `PLAN`, `DECOMPOSE` | re-run the agent (the crash happened before checkpoint, so no result was saved); cost is the only loss |
| `CODE` | the task is `in_progress`; edits and commits already in the worktree survive; the Coder is told "a previous attempt may have left partial changes; check git_status first" |
| `TEST` | just re-run |
| `PR` | `open_pr` is idempotent by branch; `push_branch` is a normal push |

**Tests:** unit — `save`/`load_latest` round-trip through JSON keeps every field (compare `model_dump()`); integration — start a run with fake agents in a subprocess, `SIGKILL` it after the `PLAN` checkpoint, call `resume`, assert the run finishes and `steps` contains one `planner` row.

---

### Step 2.7 — Worker leases, retries, lock renewal, cancel

**Files:** `orchestrator/worker.py`, `orchestrator/lease.py`, `tests/integration/test_worker.py`.

- arq: `max_tries = 3`, `job_timeout = 50 * 60`, `retry_jobs = True`. `run_job` calls `resume(run_id)` unconditionally; a fresh run has no checkpoint and starts at `SETUP`.
- Lock renewal: `lease.py` runs `renew_lock` every 20 s in a background task while the run is active; on process death the 60 s TTL frees the repo for the retry.
- Cooperative cancel: `RedisBus.set_cancel` (Phase 0) is checked in three places — the runner before each node, `OrchestratorHooks.before_tool`, and the `awaiting_input_node` wait loop. On cancel: `state.phase = FAILED`, `runs.status = "cancelled"`, teardown removes the container.
- Worker identity: `worker_id = f"{hostname}:{pid}"` is the lock owner and is stored on `runs.worker_id` (new nullable column, migration `0002`).

**Tests:** cancel flag set during a slow fake node ends the run as cancelled; lock TTL expires when renewal stops.

---

### Step 2.8 — Events and SSE

**Files:** `orchestrator/events.py`, `api/routes/events.py`, `cli/main.py` (`watch`), `tests/integration/test_events.py`.

- `emit(bus, db, run_id, type, payload)` writes the Redis Stream entry (README §4.8) and the `events` row in one call. Event types are the README §10 list. Payloads are small: tool inputs are truncated to 2 KB, outputs to 1 KB; full content lives in `tool_calls`/`artifacts`.
- `agent_text`: the Anthropic provider streams; batch text deltas every ~250 ms per message into `agent_text` events (`{"step_id", "delta"}`). Thinking is requested with `display: "summarized"` on Opus/Sonnet routes and forwarded as `agent_text` with `{"kind": "thinking"}` so the UI can show the agent reasoning.
- `GET /runs/{id}/events` (`sse-starlette`): if the run is terminal, replay from the `events` table and close. Otherwise, start from `Last-Event-ID` (a stream id) or `0-0`, `XREAD BLOCK 15000`, send a `: keepalive` comment on timeout, stop after `run_finished`. Auth as for other routes.
- `autoswe watch <id>` renders the stream: phase banners, tool calls one-line each, test summaries, and `awaiting_input` prompts in colour.

**Tests:** emit three events, connect with `Last-Event-ID` of the first → receive two; terminal run replays from the table.

---

### Step 2.9 — `AWAITING_INPUT`, answers, cancel endpoint

**Files:** `orchestrator/nodes.py` (`awaiting_input_node`), `api/routes/control.py`, `cli/main.py` (`answer`, `cancel`), `tests/integration/test_awaiting_input.py`.

- `awaiting_input_node`: emits `awaiting_input` with `{"kind": "open_questions", "questions": [...]}`, sets `runs.status="awaiting_input"`, then loops: `pop_inbox(run_id, timeout_s=30)` → on message `{"type": "answer", "text"}` append `(questions_joined, text)` to `state.answers`, set `state.phase = Phase.PLAN` (transition special-case), return. Between polls: renew the lock, check cancel, and stop waiting after `AWAITING_INPUT_TIMEOUT_S` (default 24 h) → `FAILED` with error `no answer`. Wall-clock spent waiting is excluded from the budget (Phase 3 uses `state.waiting_s`, so record it now).
- `POST /runs/{id}/answer {text}` → 409 if the run is not `awaiting_input`, else `push_inbox` and 202.
- `POST /runs/{id}/cancel` → `set_cancel`, 202.

**Tests:** a plan with questions parks the run; posting an answer resumes `PLAN` with the Q&A in the step input; cancel while parked ends the run as cancelled.

---

### Step 2.10 — Multi-task `CODE`/`TEST` loop and transition v2

**Files:** `orchestrator/nodes.py`, `orchestrator/transition.py`, `tests/unit/test_transition.py`.

- `code_node`: `task = state.tasks.next_ready()`; mark `in_progress`; `state.current_task_id`; fresh `RunContext`; user message = goal summary (2 lines), the task (title, description, acceptance criteria, files, test selector), profile conventions, and the contents of up to 6 files named in `task.files` (fenced, each capped at 300 lines; larger files get a `read_file` hint instead). The Coder does not see previous tasks' transcripts; it sees `git_status` if it asks. Result → `state.task_results[task.id]`.
- `test_node`: `run_tests(selector=task.test_selector)`; if that passes, `run_tests()` with no selector (full suite). Store the last report; on pass, mark the task `done`.
- `transition` v2:

```python
def transition(s: RunState) -> Phase:
    match s.phase:
        case Phase.SETUP:      return Phase.ANALYZE
        case Phase.ANALYZE:    return Phase.PLAN
        case Phase.PLAN:       return Phase.AWAITING_INPUT if s.plan.open_questions else Phase.DECOMPOSE
        case Phase.AWAITING_INPUT: return Phase.PLAN
        case Phase.DECOMPOSE:  return Phase.CODE
        case Phase.CODE:       return Phase.TEST if s.current_task_id in s.task_results else Phase.FAILED
        case Phase.TEST:
            if s.last_test_report.passed:
                return Phase.CODE if s.tasks.next_ready() else Phase.PR
            return Phase.FAILED                       # Phase 3 replaces this line with DEBUG/ESCALATE
        case Phase.PR:         return Phase.DONE
    raise ValueError(s.phase)
```

**Tests:** extend the table: three tasks cycle `CODE→TEST→CODE→TEST→CODE→TEST→PR`; open questions route through `AWAITING_INPUT` and back; a task without a result fails.

---

## 4. Testing plan

| Tier | Coverage | Command |
|---|---|---|
| unit | fs/search tools, profile detection, repo map, transition v2, checkpoint round-trip, agents with fake provider | `make test` |
| integration | resume after SIGKILL, SSE replay, awaiting-input round trip, cancel, worker lease expiry | `make test-int` |
| e2e | fixture goal with ≥3 tasks; a goal with an intentional ambiguity ("add auth" with no scheme named) to see the pause | `uv run pytest -m e2e tests/e2e/test_m2.py` |

---

## 5. Demo script

```bash
uv run autoswe run --repo https://github.com/<you>/autoswe-fixture-python \
  --goal "Add a Stats class in fixture/stats.py with mean(), median(), and mode(), a CLI entry point fixture/__main__.py that reads numbers from stdin and prints all three, and tests for each."
uv run autoswe watch <id>              # in another terminal
# wait for: phase_changed → plan ; phase_changed → decompose ; tasks: 3–4
kill -9 $(pgrep -f "arq orchestrator.worker")      # during CODE of task 2
uv run arq orchestrator.worker.WorkerSettings &     # arq retries the job; resume() picks up the checkpoint
# watch continues from the same task; steps table shows one planner row, one decompose row
psql "$DATABASE_URL" -c "select seq, phase from checkpoints where run_id='<id>' order by seq"
```

Second demo, open questions:

```bash
uv run autoswe run --repo … --goal "Add authentication"
uv run autoswe watch <id>              # → awaiting_input: "Which scheme: session cookies or JWT? …"
uv run autoswe answer <id> "JWT with HS256, secret from env JWT_SECRET, access tokens only"
```

---

## 6. Pitfalls

- **The planner asks too many questions.** Give it explicit examples of non-blocking ambiguity and an instruction to state assumptions. Measure: on the fixture suite, fewer than one in five runs should pause.
- **Checkpoint JSON drift.** Every `RunState` field must survive `model_dump(mode="json")` → `model_validate`. UUIDs, datetimes, and tuples are the usual suspects; the round-trip test catches them.
- **Resume re-runs an agent that already produced a result.** Only if the crash happened between agent return and checkpoint. Acceptable; log it as `resume.reran_step`.
- **SSE through proxies.** Send the keepalive comment and disable response buffering (`X-Accel-Buffering: no`).
- **Streaming plus the tool runner.** If forwarding text deltas from inside the runner is awkward in your SDK version, emit one `agent_text` per assistant message in this phase and revisit per-token streaming in Phase 5. Do not block the phase on it.
- **Coder context size.** Six fenced files of 300 lines is roughly 20–30K tokens. Cap it; the Coder has `read_file` for the rest.
- **Lock owner.** Two workers retrying the same job after a crash must not both run it. The lock owner is the worker id and `renew`/`release` are owner-checked Lua scripts (Phase 0).

---

## 7. Checklist before Phase 3

- [x] Exit criteria in §1 all ticked; the SIGKILL test is in CI (`integration`). — All but the multi-task e2e; see the provider note in §1. CI now builds the sandbox image before the integration tier, because without it every sandbox test skipped and the job went green while proving nothing.
- [x] `RunState` has `attempts`, `waiting_s`, and `last_test_report` populated — Phase 3 reads them. — `attempts` per task in `code_node`, `waiting_s` accumulated on every exit from `awaiting_input_node`, `last_test_report` set by `test_node`.
- [x] You can explain the idempotency rule per node and where resume re-attaches the container. — A checkpoint is written after every node, so a crash re-runs at most one. `resume.reattach` takes the repo lock, reuses the worktree directory when it is still there, then throws away the old container and starts a fresh one: a container from a dead attempt cannot be trusted, but the worktree is on disk and holds the run's commits.
- [ ] Tag `v0.2.0`.
