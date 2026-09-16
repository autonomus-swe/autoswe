# Results

`m3.jsonl` is written by `tests/e2e/test_m3.py`, one line per scenario run: attempts,
tokens, cost, phase, and which tests were excused. Crashes get a row too — a run that
raised is the outcome most worth having in the record.

It is not in the repository yet, because no run has produced a usable row.

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
