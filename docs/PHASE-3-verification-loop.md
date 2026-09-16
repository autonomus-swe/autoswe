# Phase 3 — The Verification Loop (README milestone M3)

> Outcome: the loop that makes the PR actually green. Tests are parsed into structured reports with stack frames, a Debugger forms a hypothesis before it edits, attempts are budgeted, repeated failures are detected, and when the loop cannot converge the run escalates — replan, ask a human, or stop — instead of spinning. Demo line: "Inject a failing test; watch it debug and recover."

Suggested duration: 5–7 days. This phase is the core of the resume claim; the transition table gets more tests than any other module.

---

## 0. Where this phase sits

**Prerequisites**
- Phase 2 exit checklist ticked (`v0.2.0`). Resume, events, and `AWAITING_INPUT` work.
- A second fixture repository with deliberately broken states (Step 3.9).

**Delivers**
- `tools/tests.py` (v2: JUnit/json-report parsing into frames and signatures), `repo/source_context.py`
- `orchestrator/nodes.py`: `test_node` (v2 with baseline and flaky detection), `debug_node`, `escalate_node`
- `agents/debugger.py`, `agents/prompts/debugger.md`, `agents/tester.py` (triage helper)
- `orchestrator/transition.py` (v3: the README §5 table minus the Phase 4 review/security rows)
- `tools/ask_user.py`, `orchestrator/approvals.py` (approval gate inside the tool loop), `tools/policy.py` (ASK list)
- `api/routes/control.py`: `POST /runs/{id}/approve`, `POST /runs/{id}/reject`
- `gateway/budget.py` (v2: enforcement from `llm_calls`, wall-clock, warnings, downgrade hook)
- `contracts`: `Task.task_start_sha`, `Task.human_hint`, `RunState.previous_failure_signature`, `RunState.strategy`, `RunState.escalation_reason`, `RunState.waiting_s`, `RunState.baseline_failures`
- Chaos fixture repo and `tests/e2e/test_m3.py`

**Deliberately deferred**
- Review and security fix rounds, draft PRs on escalation, PR Writer (Phase 4). In this phase an escalated run that cannot recover ends `FAILED` with artifacts; Phase 4 turns that into a draft PR.
- Model downgrade at 90 % budget (Phase 5 routing); this phase only emits the warning and escalates at 100 %.

---

## 1. Exit criteria

- [x] `TestReport.failures[*].frames` are populated for pytest failures, with `in_repo` correct and `signature` stable when line numbers shift (tests exist).
- [x] The Debugger cannot call a mutating tool before `submit_hypothesis` (test exists); every `debugger` step stores its hypothesis in `steps.output`.
- [~] Fixture (a) off-by-one: recovered in one debug attempt. Fixture (c) impossible test: three distinct hypotheses, then `ESCALATE` → replan → `ESCALATE` → `awaiting_input`; in unattended mode → `FAILED`. Both are e2e tests.
  **Written, attempted, not yet passed.** `tests/e2e/test_m3.py` exists and asserts exactly this. Attempted 2026-09-16 with `qwen2.5:7b` on local Ollama: the run reached `DECOMPOSE` and the model returned prose instead of the forced `TaskGraphSpec` call, twice. A model-capability limit, matching what Phase 2 measured about 7B models and nested schemas — see `evals/results/README.md`. Everything up to the point a model is asked to think is verified without one, in two layers: `tests/integration/test_chaos_fixtures.py` on the host and `tests/integration/test_chaos_in_sandbox.py` in the real sandbox.
- [x] Same failure signature twice → the next Debugger prompt contains the "alternative strategy" block (test exists).
- [x] Pre-existing failing tests recorded in `SETUP` do not block a run (fixture (e), test exists).
- [x] Wall-clock, dollar, and token budgets each produce `budget_warning` at 90 % and `ESCALATE` at 100 % (table tests with fake usage).
- [x] A bash command on the ASK list pauses the run; `POST /approve` runs it, `POST /reject` returns the reason to the model (integration test).
- [x] `transition()` has a table test with at least 25 rows and 100 % branch coverage.
- [x] Tag `v0.3.0`.

---

## 2. Architecture slice

README §5, implemented in full except the review/security rows:

```
CODE(t) ──► TEST(t) ──passed──► next task / PR
              │ failed
              ▼
   attempts[t] < 3 ? ──no──► ESCALATE ──► replan once ──► CODE
              │ yes                  └──► ask_user ──► DEBUG (hint)
              ▼                      └──► (unattended) FAILED
   same signature as last time? ──yes──► strategy = "alternative"
              ▼
           DEBUG: submit_hypothesis ──► edit ──► run failing tests ──► full suite ──► commit ──► submit_result
              │
              └──► TEST
```

---

## 3. Work breakdown

### Step 3.1 — Test report parsing v2: frames, kinds, signatures

**Files:** `tools/tests.py`, `tools/test_parsers/pytest_json.py`, `tools/test_parsers/junit.py`, `repo/source_context.py`, `tests/unit/test_test_parsers.py` with saved reports under `tests/fixtures/reports/`.

`pytest --json-report` gives, per test, `outcome`, `call.longrepr` (text), `call.crash {path, lineno, message}`, and `call.traceback [{path, lineno, message}]`. Parse into `Frame`s:

- `file`: path relative to the worktree when under it; `in_repo = path is under worktree and not under .venv/, site-packages/, /usr/`.
- `line`: from the traceback entry.
- `function`: parse `longrepr` for lines of the form `path:line: in func_name` (pytest's long representation); fall back to `"?"`.
- `code`: read the line from the worktree on the host (`source_context.line_at(path, line)`); never from the model.

Kind classification (first match):

| Evidence | kind |
|---|---|
| `crash.message` starts with `AssertionError` or longrepr contains `assert ` with `E   ` lines | `assertion` |
| `ModuleNotFoundError`, `ImportError`, collection error | `import` |
| `Timeout` from `pytest-timeout`, exec `timed_out=True` | `timeout` |
| `ConnectionError`, `PermissionError`, `FileNotFoundError` for paths outside the worktree, missing binary, `OSError` in setup | `environment` |
| anything else | `exception` |

Signature (stable across edits):

```python
def signature(test_id: str, kind: str, exc_type: str, frames: list[Frame]) -> str:
    top = next((f for f in frames if f.in_repo), None)
    key = f"{test_id}|{kind}|{exc_type}|{top.file if top else ''}|{top.function if top else ''}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]
```

Line numbers are deliberately excluded: the Debugger edits code, lines move, the failure is still "the same" if the test, kind, exception type, and function match. `TestReport` gets a report-level `signature` = sha1 of the sorted failure signatures (add to the contract).

JUnit XML parser (`--junitxml` for non-pytest runners, used by Phase 5's node/go images): `<testcase>` → `<failure message>` text → frames by regex on common traceback formats; `kind` from the message. Lower fidelity is fine.

`source_context.around(path, line, radius=10) -> str` returns numbered lines for the Debugger; the tester node calls it for up to three in-repo frames per failure and stores the result on the failure as runtime context (`TestReportContext`, a `StateModel` keyed by `test_id`; not in the LLM schema).

**Tests:** saved reports: assertion with two frames (one in `.venv`), import error at collection, timeout, environment (`ConnectionRefusedError`); signature unchanged when the frame line changes; JUnit sample from `go test -json | go-junit-report` and from `jest --ci --reporters=jest-junit`.

---

### Step 3.2 — Tester node v2: baseline, selector-then-suite, flaky detection

**Files:** `orchestrator/nodes.py` (`test_node`, `setup_node` baseline), `agents/tester.py`, `tests/unit/test_tester_node.py`.

- **Baseline in `SETUP`.** After install, run the full suite once. Store `state.baseline_failures = {signature, …}` and a `baseline_report` artifact. Many real repositories have failing or environment-dependent tests; the agent is only responsible for tests it touched or introduced.
- **Two runs per task.** `run_tests(selector=task.test_selector)` first (fast, targeted). If it passes, `run_tests()` for the whole suite. A full-suite failure whose signature is in `baseline_failures` is filtered out and does not fail the report (`TestReport.passed` is computed after filtering; keep the raw report as an artifact).
- **Flaky detection (deterministic).** If the full suite fails only in tests outside the selector, re-run just those tests once. If they pass, tag them `flaky` in the report context, log `test.flaky`, and treat the report as passed. Emit a `test_report` event with `{"flaky": [...]}` so it is visible in the PR later.
- **Triage helper (optional, Haiku 4.5).** `agents/tester.py: classify_unknown(failures) -> dict[test_id, FailureKind]` for failures the parser labelled `exception` with an empty message. Skip it when everything is classified; it exists so the Debugger never receives a bare "exception" with no evidence.
- The node stores `state.last_test_report` (filtered) and `state.test_context` (per-failure source context) and emits `test_report`.

**Tests:** baseline filtering; flaky re-run passes → report passed; selector failure short-circuits the full run.

---

### Step 3.3 — Debugger agent

**Files:** `agents/debugger.py`, `agents/prompts/debugger.md`, `orchestrator/nodes.py` (`debug_node`), `orchestrator/hooks.py` (hypothesis gate), `tests/unit/test_debugger.py`.

Role `debugger` (Opus 5, xhigh). Tools: everything the Coder has plus `submit_hypothesis` (schema `DebugHypothesis`). Order is enforced in `OrchestratorHooks.before_tool`: for role `debugger`, any tool with `mutating=True` is denied with "call submit_hypothesis first" until `ctx.submitted["hypothesis"]` exists.

User message:
1. The task (title, description, acceptance criteria).
2. The failing report rendered as a table: test id, kind, message, then each in-repo frame with its 20 lines of context (fenced as untrusted).
3. Previous attempts on this task: each stored hypothesis (`failure_class`, `root_cause`, `plan`, outcome) — so attempt 3 knows what attempts 1 and 2 believed.
4. If `state.strategy == "alternative"`: a block stating that the last hypothesis produced the same failure, that the same class of fix is off the table, and that the new hypothesis must name what was wrong with the previous one.
5. If `task.human_hint` is set: "A human left this note: …".

Prompt rules:
- Form the hypothesis from evidence in the frames and messages. Verify it by reading code (`read_file`, `search_code`) before submitting; `confidence` below 0.5 means "read more first".
- `failure_class` must match the report's `kind` unless the Debugger explains why the parser was wrong.
- After editing: run the failing tests only (`run_tests(selector="tests/x.py::test_y tests/x.py::test_z")`), then the full suite. Do not submit a result while the failing selector still fails unless stuck — say so in the summary.
- `environment` failures: do not "fix" by editing tests or adding `skip`; report what the environment lacks in `submit_result.notes_for_reviewer` and stop.
- Never delete or weaken a test to make it pass.

`debug_node`: `state.attempts[task.id] += 1`; start a step with `attempt`; run the agent; store `hypothesis` and `task_result` on the step; emit `debug_hypothesis`; on success `state.task_results[task.id]` is replaced.

**Tests:** fake provider calling `bash` before `submit_hypothesis` gets a denial and then succeeds after submitting; prompt contains the alternative block when `strategy` is set; previous hypotheses render in order.

---

### Step 3.4 — Transition v3 and its table

**Files:** `orchestrator/transition.py`, `tests/unit/test_transition.py`.

```python
MAX_DEBUG_ATTEMPTS = 3

def transition(s: RunState) -> Phase:
    if s.cancelled:                                  return Phase.FAILED
    if s.budget.exceeded(s.usage, s.elapsed_s()):    s.escalation_reason = s.budget.reason(...); return Phase.ESCALATE
    match s.phase:
        case Phase.SETUP:          return Phase.ANALYZE
        case Phase.ANALYZE:        return Phase.PLAN
        case Phase.PLAN:           return Phase.AWAITING_INPUT if s.plan.open_questions else Phase.DECOMPOSE
        case Phase.AWAITING_INPUT: return s.resume_phase                      # PLAN, or DEBUG after an escalation answer
        case Phase.DECOMPOSE:      return Phase.CODE
        case Phase.CODE:           return Phase.TEST if s.current_task_id in s.task_results else Phase.ESCALATE
        case Phase.TEST:
            r = s.last_test_report
            if r.passed:
                s.previous_failure_signature = None; s.strategy = None
                return Phase.CODE if s.tasks.next_ready() else Phase.PR        # Phase 4: REVIEW
            if s.attempts.get(s.current_task_id, 0) >= MAX_DEBUG_ATTEMPTS:
                s.escalation_reason = "debug_attempts_exhausted"; return Phase.ESCALATE
            s.strategy = "alternative" if r.signature == s.previous_failure_signature else None
            s.previous_failure_signature = r.signature
            return Phase.DEBUG
        case Phase.DEBUG:          return Phase.TEST
        case Phase.ESCALATE:       return s.resume_phase                      # set by escalate_node: CODE, AWAITING_INPUT, or FAILED
        case Phase.PR:             return Phase.DONE
    raise ValueError(s.phase)
```

`transition` may set bookkeeping fields on `s` (`strategy`, `previous_failure_signature`, `escalation_reason`); it never performs I/O and never reads anything except `s`. That is what keeps it table-testable.

The table test builds `RunState` fixtures and asserts the next phase for at least these rows: each linear edge; pass with next task / without; fail attempts 0, 1, 2 (→ DEBUG) and 3 (→ ESCALATE); same signature twice → `strategy == "alternative"`; different signature → `None`; budget exceeded at every phase → ESCALATE with the right reason; cancelled → FAILED; `CODE` without a result → ESCALATE; `AWAITING_INPUT` resumes to `PLAN` and to `DEBUG`.

---

### Step 3.5 — Escalation policy

**Files:** `orchestrator/nodes.py` (`escalate_node`), `agents/decomposer.py` (`replan_task`), `tests/unit/test_escalate.py`.

`escalate_node` decides `state.resume_phase` by reason:

| `escalation_reason` | Condition | Action | resume_phase |
|---|---|---|---|
| `debug_attempts_exhausted` | task not yet `replanned` | revert the worktree to `task.task_start_sha` (`git reset --hard`, then `git clean -fd` excluding `.venv`), call `replan_task(task, failures, hypotheses)` → 1–3 smaller `TaskSpec`s replacing the task in the graph, `replanned=True`, attempts reset | `CODE` |
| `debug_attempts_exhausted` | already replanned, `unattended=False` | emit `awaiting_input {kind: "escalation", summary, hypotheses}`; wait on the inbox; the answer becomes `task.human_hint`, `attempts[task] = MAX-1` (one more try) | `AWAITING_INPUT` (then `DEBUG`) |
| `debug_attempts_exhausted` | already replanned, `unattended=True` or answer timeout | `finish_run(status="failed", error=…)` | `FAILED` |
| `budget_*` | any | `finish_run(status="failed")`; Phase 4 opens a draft PR here | `FAILED` |
| `coder_no_result` | `CODE` returned nothing | treat like one failed attempt: `attempts[task] += 1`; back to `CODE` if under the cap else the rows above | `CODE` |

`task_start_sha` is recorded by `code_node` before the Coder starts (`git rev-parse HEAD` on the worktree). `replan_task` reuses the Decomposer with a focused prompt: "this task failed three times; here is what was tried; split it into smaller steps or a different approach; the new tasks replace it and must keep its acceptance criteria collectively."

**Tests:** each row with fakes; the worktree reset is verified on a temp git repo.

---

### Step 3.6 — `ask_user`, approvals, and the ASK list

**Files:** `tools/ask_user.py`, `orchestrator/approvals.py`, `orchestrator/hooks.py`, `tools/policy.py`, `api/routes/control.py`, `cli/main.py` (`approve`, `reject`), `tests/integration/test_approvals.py`.

- `ask_user(question: str)` — custom tool, `requires_approval=True`, available to the Coder and Debugger (not to read-only roles). Its call function delegates to the approval gate with `kind="question"` and returns the human's answer as the tool result.
- `ApprovalGate.wait(ctx, kind, tool_name, input) -> Decision`: writes `runs.status="awaiting_input"`, emits `awaiting_input {kind, tool_call_id, tool_name, input}`, then `pop_inbox` in a loop (renew lock, check cancel, accumulate `state.waiting_s`) until a message with the matching `tool_call_id` arrives: `approve` → run the tool; `reject {reason}` → `ToolResult("user rejected: <reason>", is_error=True)`; `answer {text}` → for `ask_user`, the answer. Timeout → reject with "no response".
- `OrchestratorHooks.before_tool` calls the gate when `tool.requires_approval` or when `policy.needs_approval(command)` matches the ASK list for bash:

```python
ASK = [
    (r"\brm\s+-r", "recursive delete inside the workspace"),
    (r"\b(uv|pip|npm|pnpm|yarn|go)\s+(add|install|pip install)\b", "adding a dependency"),
    (r"\balembic\s+downgrade\b", "database downgrade"),
    (r"\bgit\s+", None),   # never reaches here; DENY runs first
]
```

- Endpoints: `POST /runs/{id}/approve {tool_call_id}`, `POST /runs/{id}/reject {tool_call_id, reason}` → 409 unless the run is `awaiting_input` with that pending id (the pending id is stored in Redis `run:{id}:pending`).
- Unattended runs auto-reject ASK matches with "unattended run: not permitted" so the model works around them.
- README §4.2 calls interrupts "states". For open questions and escalations the phase is literally `AWAITING_INPUT`. For tool approvals the agent step is mid-flight, so the run *status* is `awaiting_input` while `RunState.phase` stays `CODE`/`DEBUG`; both are visible on `GET /runs/{id}`. Document this in `docs/`.

**Tests:** a fake coder issues `uv add requests` → run pauses → `approve` executes it → the tool result is the exec output; `reject` → the model receives the reason; unattended → auto-reject without pausing.

---

### Step 3.7 — Budgets: enforcement from `llm_calls`

**Files:** `gateway/budget.py`, `contracts/budget.py`, `orchestrator/hooks.py`, `tests/unit/test_budget.py`.

```python
class Budget(StateModel):
    max_debug_attempts: int = 3
    max_fix_rounds: int = 2
    wall_clock_s: int = 45 * 60
    max_usd: float = 10.0
    max_tokens: int | None = None
    warn_at_fraction: float = 0.9
    def fraction_used(self, usage: Usage, elapsed_s: float) -> dict[str, float]: ...
    def exceeded(self, usage, elapsed_s) -> bool: return max(self.fraction_used(...).values()) >= 1.0
    def reason(self, usage, elapsed_s) -> str:  # "budget_usd" | "budget_wall_clock" | "budget_tokens"
```

- `Usage` is reconciled from the database: `state.usage = await run_cost(run_id)` at every node boundary and inside `on_message` every 10 turns. In-memory counters are only a cache. This is the README rule "budgets are enforced from `llm_calls`".
- `elapsed_s = now - started_at - waiting_s`. Time spent waiting for a human never counts.
- Warnings: when any fraction crosses `warn_at_fraction` the first time, emit `budget_warning {kind, fraction}` once (`state.warned: set[str]`). Phase 5 hooks model downgrade to this event.
- Mid-step exhaustion: `before_tool` denies mutating tools with "budget exhausted: commit what is consistent and call submit_result" and lowers the runner's remaining iterations to 3. The next `transition()` sends the run to `ESCALATE`.

**Tests:** fractions and reasons for each budget; warning emitted once; exhaustion mid-step denies mutating tools but allows `git_commit` and `submit_result`.

---

### Step 3.8 — Cancel hardening

**Files:** `orchestrator/runner.py`, `sandbox/docker.py`, `tests/integration/test_cancel.py`.

- A cancel during a long `exec` must not wait for the timeout: the runner's cancel watcher calls `sandbox.kill_exec()` (implemented as `container.kill()` followed by teardown — the run is over anyway).
- A cancel during `AWAITING_INPUT` or an approval wait returns immediately (the inbox loop checks the flag every poll).

**Verified in the running product, 2026-09-16.** `autoswe cancel` during `ANALYZE`: the
watcher noticed in ~2 s and killed the sandbox (`cancel_killing_sandbox` →
`sandbox_exec_killed`), the run ended `cancelled` with "cancelled while a phase was
running", the container was removed and the repo lock released.

**What it does not stop: an in-flight request to the model.** The 14 s between the kill and
the run ending was the provider call unwinding. The watcher kills the sandbox, not the HTTP
request, so a cancel during a model call waits for that call — bounded only by
`llm_timeout_s`, which defaults to 600 s and is set to 5400 s in some local setups. Worth
closing in Phase 5 (the provider call would need to take a cancellation token), and worth
knowing now: a cancel is prompt against commands and patient against models.

---

### Step 3.9 — Chaos fixtures and e2e

Create `github.com/<you>/autoswe-fixture-bugs` with one branch per scenario (the run's `base_branch` selects it):

| Branch | Scenario | Expected path |
|---|---|---|
| `a-off-by-one` | `paginate()` returns one item too few; test exists | CODE → TEST fail → DEBUG (1) → TEST pass |
| `b-missing-import` | new module forgets `from datetime import datetime` | DEBUG fixes an `import` kind in 1 attempt |
| `c-impossible` | a test asserting two contradictory things; goal says "do not change tests" | 3 hypotheses → ESCALATE → replan → 3 more → `awaiting_input`; unattended → FAILED |
| `d-network` | a test that calls `https://example.com` | `environment` kind; Debugger reports it, does not skip the test; run escalates with a clear reason |
| `e-baseline` | an unrelated test already failing on the base branch | baseline filter; run completes; the PR later lists the pre-existing failure |

`tests/e2e/test_m3.py` runs (a), (c) unattended, and (e). Record attempts and cost per scenario in `evals/results/m3.jsonl`; these numbers are the first ones you will quote.

---

## 4. Testing plan

| Tier | Coverage | Command |
|---|---|---|
| unit | parsers, signatures, transition table (≥25 rows), escalation rows, budget math, hypothesis gate, prompt blocks | `make test` |
| integration | approvals round trip, cancel during exec, worktree reset on replan, baseline run in SETUP | `make test-int` |
| e2e | fixtures (a), (c), (e) | `uv run pytest -m e2e tests/e2e/test_m3.py` |

---

## 5. Demo script

```bash
uv run autoswe run --repo https://github.com/<you>/autoswe-fixture-bugs --base a-off-by-one \
  --goal "Make tests/test_paginate.py pass without changing the tests."
uv run autoswe watch <id>
# test_report: failed=1 kind=assertion  → debug_hypothesis: "range end is exclusive; loop stops one short" (confidence 0.9)
# tool_call run_tests selector=tests/test_paginate.py::test_last_page → passed → full suite → passed → pr_opened

uv run autoswe run --repo … --base c-impossible --goal "Make tests/test_impossible.py pass without changing the tests."
uv run autoswe watch <id>
# three hypotheses, then: phase_changed → escalate (debug_attempts_exhausted) → replan → … → awaiting_input (escalation)
uv run autoswe answer <id> "The test is wrong; leave it failing and document why."
# → debugger submits a result with notes_for_reviewer; run ends FAILED with artifacts (Phase 4 makes this a draft PR)
```

---

## 6. Pitfalls

- **Signatures that include line numbers.** They change on every edit and defeat no-progress detection. Use test id, kind, exception type, and top in-repo function.
- **The Debugger "fixes" tests.** Say it explicitly in the prompt and check in the Reviewer (Phase 4). Track a metric: diffs that touch `tests/` during `DEBUG` when the task did not ask for it.
- **Baseline runs cost time.** On a big suite the baseline can take minutes. Cache it by `base_sha` in `artifacts` so repeated runs on the same commit skip it.
- **Two counters, one meaning.** `attempts[task]` is incremented in `debug_node`, and nowhere else. If you also increment it in the transition you will escalate one attempt early; the table test row "attempt 2 → DEBUG" catches it.
- **Approval waits and arq timeouts.** A human may take an hour. Either raise `job_timeout` (simplest) or make the wait release the job. Wall-clock budget already excludes waiting; make sure the arq timeout does too.
- **Budget checks inside the tool loop.** Without the mid-step denial, a Coder can spend twice the budget in one long step before the transition looks at it.

---

## 7. Checklist before Phase 4

- [~] Exit criteria in §1 all ticked; `evals/results/m3.jsonl` has the five scenarios with attempts and cost. Blocked on a funded key — see `evals/results/README.md`; no numbers are recorded that were not measured.
- [x] `RunState` carries `fix_rounds` (dict, default empty) and `return_to` (None) as placeholders — Phase 4 fills them.
- [x] You can walk through the transition table row by row and explain why `transition()` is allowed to mutate bookkeeping fields but never do I/O.
- [x] Tag `v0.3.0`.
