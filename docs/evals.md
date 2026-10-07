# Evaluating the agent

Everything this project claims about itself should be traceable to a row in a JSONL file
that a run wrote. This is the harness that produces those rows.

```bash
export AUTOSWE_API_KEY=...            # the control plane's key
export AUTOSWE_FIXTURE_REPO=https://github.com/<you>/autoswe-fixture-python
uv run autoswe eval --suite private
uv run python -m evals.report evals/results/eval-private.jsonl
```

---

## 1. What "resolved" means

A command exited zero on a checkout of the branch the agent pushed.

Not that the model said it was finished. Not that a diff applies. A patch that applies
cleanly and fails its own tests is not a resolved task, and only running the tests tells
the two apart.

**A task with no verify command is `resolved: null`** — never `false`, and never `true`. It
is counted in its own column and excluded from the denominator. A suite that counted
unverifiable tasks as successes would report a hundred per cent the moment somebody forgot
a `verify:` block, and the percentage would be of a denominator that had silently changed.

`autoswe eval` exits non-zero when anything is `false`, and zero when the only non-passes
are `null`. That is deliberate: unverifiable is not a failure, it is a gap in the suite.

---

## 2. Writing a task

```yaml
# evals/tasks/private/ops-guard-zero.yaml
id: ops-guard-zero
repo: "${AUTOSWE_FIXTURE_REPO}"
base: main
goal: >-
  Add divide(a, b) to fixture/ops.py. It must raise ValueError with a clear message when b
  is zero rather than propagating ZeroDivisionError, and tests/test_ops.py must cover both
  the normal case and the zero case.
verify:
  command: "uv run pytest -q"
  expect_exit: 0
tags: [bugfix, fixture]
budget_usd: 3.0
```

| Field | |
|---|---|
| `repo` | an `https://github.com/owner/name` URL. `${VAR}` is expanded from the environment |
| `goal` | 10–4000 characters, the same bounds the control plane enforces |
| `verify.command` | run in a checkout of the agent's branch |
| `verify.expect_exit` | usually 0; a "this must keep failing" task sets it otherwise |
| `budget_usd`, `timeout_s` | per task; the run is cancelled at the timeout, not abandoned |

**Repositories come from the environment on purpose.** The agent pushes a branch and opens
a pull request, so a task has to point at a repository you control. A URL committed here
would be wrong for everyone, or an invitation to run an agent against somebody else's
repository.

**Verify the whole suite, not one file, when the task is a bugfix.** `uv run pytest -q`
rather than `pytest tests/test_one.py` — otherwise deleting the failing test is a way
through.

**Tasks are validated before any run starts.** A suite of thirty is an hour and real money;
a goal the API would refuse with a 422 on task twenty-nine stops the suite at task zero.

---

## 3. What a row records

One row per task, appended as it finishes — not written at the end, because a suite is an
hour of real runs and a harness that loses all of it to a crash on the last task is one
nobody runs twice.

```json
{"task_id": "ops-guard-zero", "resolved": true, "status": "done",
 "tasks": 2, "debug_attempts": 6, "review_rounds": 1,
 "wall_clock_s": 564.0, "cost_usd": 0.0, "cache_hit_rate": 0.617,
 "pr_url": "https://github.com/...", "provider": "openai_compat"}
```

`debug_attempts` and `review_rounds` are counted from the run's **steps**, not from a
summary field: six Debugger steps is six attempts whatever any phase log says about it.
`cache_hit_rate` comes from `/runs/{id}/detail` totals, which this phase extended to carry
it — before that the number Phase 5 leads with could not be read through the API at all.

**Failures are rows too.** A task that crashed, timed out or was cancelled gets a row
saying so. The outcome most worth having in the record is the one nobody wants to write
down.

---

## 4. Reporting

```bash
uv run python -m evals.report evals/results/eval-private.jsonl
uv run python -m evals.report evals/results/eval-private.jsonl --by provider
```

`--by <field>` renders a comparison table grouped by any field in the row — `provider`,
or an arm name you set with `--results`. The arm's label is printed, never interpreted:
what an ablation changed is a fact about how it was run, and a renderer that guessed would
eventually guess wrong in a table somebody pasted into a README.

**Every number goes into the README with its conditions.** `report.conditions()` renders
the line: model, provider, date. 60 % on three fixture tasks with a local 7B and 60 % on
thirty repository tasks with a frontier model are different claims, and a reader who
cannot tell them apart has been misled by the format rather than by the number.

Medians, not means, for attempts and wall clock. One run that hit its timeout drags a mean
somewhere no run actually went.

---

## 5. Ablations

An arm is a suite run with one thing changed, written to its own results file:

```bash
uv run autoswe eval --suite private --results baseline
uv run autoswe eval --suite private --ablate no-debugger --results no-debugger
uv run python -m evals.report evals/results/no-debugger.jsonl --by ablation
```

`--ablate` takes the arms that are a property of the **run**, so one suite can be compared
against another without restarting anything. Today that is `no-debugger`, which sets
`Budget.max_debug_attempts = 0` and sends the first failing test straight to ESCALATE. The
arm is recorded on every row, so `report.compare --by ablation` groups by a fact about how
the run was made rather than a guess.

The plan asks for five arms. Where each one lives:

| Arm | How |
|---|---|
| without the Debugger | `--ablate no-debugger` |
| without the repo map | `REPO_MAP_VERSION=v1` on the **worker**, then restart it |
| Opus vs Sonnet as Coder | needs two model tiers configured; a single-model deployment cannot distinguish them |
| Claude vs an open model | needs a second provider; this build has one |
| effort `high` vs `xhigh` | needs a provider that takes an effort parameter |

`no-repomap` is deliberately absent from `--ablate`: the repo map version is a worker
process setting, not a run field, and offering it as a run flag that quietly did nothing
would produce two identical columns with different labels.

**Say which arms you ran.** Three of the five cannot be distinguished on a single-model
deployment, and a comparison table that silently omits them reads as though they were
tried.

---

## 6. Judging pull-request descriptions

```python
from evals.judge import judge_many
```

Four criteria, one to five, each with a reason: accuracy against the diff, completeness of
the testing section, honesty about known issues, concreteness of the rollback. Accuracy is
the one that matters and the only one a reader cannot check cheaply — a description of a
change that was *planned* rather than made scores 1 however well written it is.

**This departs from the plan.** It scores these with Opus through the Anthropic Message
Batches API at half price; this build has no Anthropic provider and is not getting one, so
the rubric goes through whatever `LLM_PROVIDER` is configured, one call per run.

**A judge on the same model as the worker is a weak judge.** It is not independent and it
shares the blind spots of the thing it is grading. Point it at a different model where you
have one, and say which model judged beside the scores.

---

## 7. SWE-bench Lite

```bash
uv pip install datasets swebench          # deliberately not dependencies of this project
.venv/bin/python -m evals.swebench --limit 5         # no fork needed; see below
python -m swebench.harness.run_evaluation \
    --predictions_path evals/results/predictions.jsonl --run_id <id>
```

**`.venv/bin/python`, not `uv run`.** `datasets` is not in `pyproject.toml` on purpose, and
`uv run` syncs the environment to the lock file before it runs anything — which removes the
package you just installed, and reports it as absent on the next invocation. The venv
interpreter does not sync. Measured here, after the documented two-line sequence failed with
its own "SWE-bench needs the `datasets` package" message.

**`--concurrency` is per repository, and the default of 1 is the safe one.** SWE-bench Lite
is grouped by repository — the first ten instances are six astropy and four django — and the
orchestrator locks per `repo_url@base_branch`, so two instances from one repository cannot
run at once. `predict` now takes a lock per repository under the global ceiling, so different
repositories overlap and the same repository serialises. Before that, `--limit 5
--concurrency 2` failed four of its five instances in SETUP with *"another run holds
…/astropy@main"* — and an empty `model_patch` is a legitimate "not solved", so those lock
errors were indistinguishable in the results file from a model that could not do the task.

This **produces patches and does not score them**. Every instance needs a specific Python
version and pinned dependencies, and the official harness has an environment image per
instance to provide them. Making this project's sandbox images solve that would be
reimplementing SWE-bench in order to run SWE-bench, and the score would be against our
reconstruction of the environment rather than against the benchmark.

**No fork is needed.** The `diff` artifact is written in TEST — so a run against
`django/django` produces its patch, then fails at the push because you do not own the
repository, and the patch is already saved. The producer takes it whatever the run's final
status is. `--fork-owner` exists for when you *do* want the runs to push somewhere, and
changes only where the clone comes from.

**The boundary, stated exactly:** the diff is stored when the final task's tests *pass*.
A run that never gets there holds no diff, and an empty patch is the honest prediction —
ESCALATE rewinds a task before replanning it, so by the end there is nothing in the
worktree to salvage. That is the ordinary outcome on a hard instance with a small model,
and `docs/numbers.md` has one measured end to end.

That means a `failed` status in `predictions.jsonl` is not "unsolved": the `model_patch`
field is what says so. Both travel, so a reader can tell "solved it and could not push"
from "produced nothing".

**Each run starts at the instance's pinned commit.** `RunCreate.base_commit` is what makes
a score meaningful: without it every run started from a branch head, a patch produced there
does not apply to the instance's base, and the harness would report failures caused by the
wrong starting point.

The mirror fetches branch heads, so an instance whose `base_commit` is reachable from no
branch fails in SETUP with a message saying exactly that. Its prediction is an empty patch,
which is the honest answer: not solved, and the row says why.

An empty `model_patch` is a legitimate prediction meaning "not solved". Omitting the row
would shrink the denominator, which is a way of improving a score by not reporting the
attempts that failed.

**Run five before you run fifty.** Fifty instances at a few dollars each is real money.

---

## 8. What this build can and cannot measure

| | |
|---|---|
| Private suite, resolved / cost / attempts | yes, needs a fork and a key |
| Cache hit rate per run | yes, from `/runs/{id}/detail` |
| PR-description quality | yes, with the caveat in §6 |
| SWE-bench Lite patches | yes, at the pinned commit, no fork needed |
| SWE-bench Lite *score* | yes, with the official harness |
| Ablation: without the Debugger | yes, `--ablate no-debugger` |
| Ablation: without the repo map | yes, by restarting the worker with `REPO_MAP_VERSION=v1` |
| Opus vs Sonnet as Coder | not on a single-model deployment |

`docs/numbers.md` is where measured figures live, each with the run that produced it.
Nothing goes in there that a JSONL row cannot back.
