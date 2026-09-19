# Results

`m3.jsonl` is written by `tests/e2e/test_m3.py`, one line per scenario run: attempts,
tokens, cost, phase, and which tests were excused. `m4.jsonl` is written by
`tests/e2e/test_m4.py`: review findings by severity, what was dropped as a false positive,
how many rejections the model explained, security findings, fix rounds, and cost. Crashes
get a row too — a run that raised is the outcome most worth having in the record.

Neither is in the repository yet, because no run has produced a usable row.

## What has been attempted

**2026-09-16, `qwen2.5:7b` via local Ollama, scenario `a-off-by-one`.** Failed at
`DECOMPOSE` after 13 minutes. The run got through SETUP, ANALYZE (with the baseline) and
PLAN; the Decomposer then returned prose — *"It seems there was an issue… ready for
execution"* — instead of calling the forced `submit_TaskGraphSpec` function, twice, so
`parse` gave up as designed.

That is a model-capability limit, not an orchestrator fault, and it matches what Phase 2
measured: a 7B local model can drive the single-agent loop but cannot reliably produce a
`TaskGraphSpec`. The phases it did reach worked — the baseline ran, the install worked,
the tests ran in the sandbox.

**2026-09-18, M1 end to end, `nvidia/nemotron-3-ultra-550b-a55b:free` via OpenRouter.**
The first run of this pipeline by a real model, and it found something eight hundred
scripted tests had not.

It got the whole way: ANALYZE, PLAN, DECOMPOSE, CODE, TEST green with all three tests
passing, REVIEW, SECURITY, and a pull request. Every gate worked. Then:

```
assert "fixture/ops.py" in changed
AssertionError: assert 'fixture/ops.py' in ''
```

The branch had **no commits**. The model wrote working code, ran the tests in the sandbox —
which tests the *worktree*, so they passed — submitted its result, and never called
`git_commit`. The run pushed a branch identical to its base, opened an empty pull request,
and reported DONE.

Every scripted provider in the suite calls `git_commit`, so no test had ever exercised an
agent simply forgetting. `pr_node` now refuses to open a pull request with no commits, and
two unit tests cover it.

**2026-09-19, the Phase 4 review criterion, `nvidia/nemotron-3-ultra-550b-a55b:free`.**
Two findings, one about the test and one about the quota.

*The test was measuring the wrong thing.* It drove a whole run with
`base_branch=g-token-expiry`, and a run reviews the diff it *produced* — `base_sha..HEAD`.
The seeded bug is in the branch relative to `main`, so the reviewer never saw it: the test
was asking whether the reviewer objects to the agent's own work. `tests/e2e/test_m4.py` now
drives the two-pass reviewer over `main..<branch>` directly, which is what the criterion
asks and costs three or four calls instead of fifty.

*The free tier is 50 requests a day.* Verbatim from the API, after the quota was spent:

    Rate limit exceeded: free-models-per-day.
    Add 10 credits to unlock 1000 free model requests per day

So the wall is a daily cap, not the model. $10 of credit raises it to 1000 requests a day
**of free models** — the credit is an unlock, not per-token spend — which covers the whole
eval matrix at no marginal cost. Until then it is roughly one full run per day, and the
run below used it.

**What this says about the free tier.** The earlier note below concluded that free API
tiers do not work. That was measured against `openrouter/free`, which is not a model id —
20 of OpenRouter's 22 free models support tool calling, and two of them produced a valid
`TaskGraphSpec` through the forced-submit path that local Ollama could not:

| model | forced `TaskGraphSpec` | time | cost |
|---|---|---|---|
| `deepseek/deepseek-v4-flash-0731:free` | 4 tasks | 41.7 s | $0.00 |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 3 tasks | 22.9 s | $0.00 |

So the pipeline can be exercised against a real model at no cost. The free tier is rate
limited, which bounds how much of the eval matrix one key can produce in a day, but it is
not the wall this file previously recorded.

**2026-09-18, the Phase 4 review criterion, local Ollama.** The seeded review pair
(`g-token-expiry`, `h-style-only`) asks whether a reviewer flags a real bug and leaves a
reformatting alone. That question does not need `DECOMPOSE` — a diff is all the reviewer
takes — so it looked answerable locally even though M3 was not. It is not, for a different
reason: **throughput**.

| measurement | result |
|---|---|
| `qwen2.5:7b`, 10-token reply, cold | 69 s |
| `qwen2.5:7b`, both scenarios, two-pass review | killed at 28 min, no pre-pass completed |
| `qwen2.5:3b`, **one** pre-pass call over a one-file diff | killed at 5 min 40 s |

So the limit here is not the forced tool call that stopped M3. A single structured call
over the smallest diff in the fixture does not finish, on the smallest model installed, in
under six minutes. Running the pair — two scenarios, a pre-pass and a tool-using
verification pass each — is hours of wall clock at best, and the verification pass has the
same forced-submit shape that M3 failed on, so it may not finish at all.

Recorded here so the next person does not spend the afternoon finding out again. The
criterion needs a hosted OpenAI-compatible endpoint; `LLM_PROVIDER=anthropic` raises
`NotImplementedError` until Phase 6, so an Anthropic key alone does not help.

**What M3 needs:** a model that reliably honours a forced function call on a nested
schema. Any funded frontier key will do; local 7B will not.

## Producing it

```bash
export LLM_API_KEY=...           # a real key: these runs call a real model
make sandbox-image               # the e2e runs need the sandbox
uv run pytest -m e2e tests/e2e/test_m3.py -s
```

Each run appends rather than overwrites, so the history of how the agent does on a
scenario survives. `AUTOSWE_EVAL_RESULTS` redirects the directory if you want to try
without touching the repository.

## What *is* verified without a model

`tests/integration/test_chaos_in_sandbox.py` installs the fixture repository in the real
sandbox and checks each scenario's report: the off-by-one arrives as two distinct
assertion signatures with both values in the message, the missing import as `import` with
the offending line, and — the one that cannot be checked anywhere else — the network test
as `environment` rather than a bug. Everything up to the moment a model would be asked to
think is covered there.
