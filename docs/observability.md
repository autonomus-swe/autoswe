# Observability

## Tracing

A run is a tree, and that shape is most of what makes a trace worth having:

```
run                               run_id, repo, provider, model, goal
└── phase.code
    └── step.coder                role, model, tier, effort, tokens, cost, cache hit rate
        ├── llm_call              model, max_tokens, tools, tokens, cost, finish reason
        │   …
        └── tool.bash             tool name
            └── sandbox.exec      command, timeout, exit code, truncated, timed out
```

It answers the question logs are worst at: *where did the forty minutes go*. A `run` span
with `phase.code` holding thirty of them, one `step.coder` inside, and forty `tool.bash`
children says immediately what a thousand log lines do not.

### Turning it on

Nothing is exported unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Spans are still created —
they are unconditional at every call site — but without an exporter they are a few
dictionary writes against a no-op span. A developer running the CLI pays nothing and needs
no collector.

Any OTLP/HTTP collector:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

### Langfuse

Langfuse speaks OTLP, so it needs no second SDK, no second set of credentials in the
process, and nothing to keep in step when either side changes:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic $(printf '%s:%s' "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" | base64 -w0)"
```

Self-hosted: the same two variables with your own host.

`llm_call` spans arrive as generations, with model, token counts and cost already on them.

**What this route gives up:** Langfuse's prompt management and scoring APIs are not
reachable over OTLP. Nothing in this project uses them. If that changes, the phase document
offers the other route — the Langfuse SDK called from `on_message` — and the spans here
would stay as they are.

### Reading a trace

- **`step.*` far longer than the `llm_call` spans inside it** — the time is in tools, not
  in the model. Look at `tool.*` and `sandbox.exec`.
- **`step_cache_hit_rate` near zero after the first step** — the cached prefix is moving.
  See `gateway/caching.py`; the usual cause is something run-specific in a role prompt.
- **`sandbox.exec` with `timed_out=true`** — the command hit its limit, and whatever the
  tool reported upward is a consequence of that rather than a real result.
- **Many `llm_call` spans under one `step.*` with a rising token count** — a loop that is
  re-sending a growing transcript. Expected for a Coder; suspicious for an Analyzer.

### What is deliberately not on a span

Prompt and completion text. A span is exported to a third-party collector, and the prompts
here carry repository contents — see the untrusted-content fence in `agents/prompts/`. The
ledger (`llm_calls`, and the artifacts table) holds what was said; the trace holds what it
cost and how long it took.

## Metrics

Prometheus counters and histograms arrive with Step 5.7b (`observability/metrics.py`,
`/metrics` on the API and `:9100/metrics` on the worker). Until then the numbers are in
`llm_calls` — `storage.repo.run_cost` per run and `storage.repo.step_costs` per step, both
carrying `cache_hit_rate`.
