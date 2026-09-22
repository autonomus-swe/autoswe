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
uv run autoswe eval --suite private --provider openai_compat --results open-model
uv run python -m evals.report evals/results/baseline.jsonl --by task_id
```

The plan asks for five arms: with and without the Debugger, with and without the repo map,
two Coder models, two providers, two effort levels. Two of those are settings this build
already has — `REPO_MAP_VERSION=v1` is the repo-map arm, and `Budget.max_debug_attempts=0`
is the Debugger arm — and the rest need model-tier configuration a single-model deployment
does not have. Run what your deployment can distinguish and say which arms you ran.

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
uv run python -m evals.swebench --limit 5 --fork-owner <you>
python -m swebench.harness.run_evaluation \
    --predictions_path evals/results/predictions.jsonl --run_id <id>
```

This **produces patches and does not score them**. Every instance needs a specific Python
version and pinned dependencies, and the official harness has an environment image per
instance to provide them. Making this project's sandbox images solve that would be
reimplementing SWE-bench in order to run SWE-bench, and the score would be against our
reconstruction of the environment rather than against the benchmark.

**`base_commit` is not honoured yet.** Each instance pins a commit and `RunCreate` takes a
branch, so a run starts from the branch head and a patch may not apply to the instance's
base. `--require-sha` refuses to produce predictions at all rather than writing a file
that will score badly for a reason the score cannot show. Until `RunCreate` accepts a SHA,
any number from this path needs that caveat printed next to it.

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
| SWE-bench Lite patches | yes; scoring is the official harness's |
| SWE-bench Lite *score* | not until `RunCreate` accepts a base commit |
| Opus vs Sonnet as Coder | not on a single-model deployment |

`docs/numbers.md` is where measured figures live, each with the run that produced it.
Nothing goes in there that a JSONL row cannot back.
