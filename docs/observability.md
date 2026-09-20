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

Self-hosted: the same two variables with your own host. For a laptop, the compose file
carries an `observability` profile — the one `PHASE-5` §5's demo script asks for, and which
until now did not exist:

```bash
docker compose --profile observability up -d langfuse   # http://localhost:3000
# create a project in the UI, then:
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3000/api/public/otel
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic $(printf '%s:%s' "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" | base64 -w0)"
```

It brings its own Postgres rather than borrowing the project's. Sharing would need a
`CREATE DATABASE` in an init script, and init scripts run only when the data directory is
empty — so anyone who has run this project before would get a Langfuse that starts and
cannot connect. The extra container is thrown away with the profile.

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

A trace tells you about one run. These tell you whether the agent is getting better or
worse across all of them, which is a different question and the only one that can justify
a change to a prompt or a route.

| Metric | Type | Labels |
|---|---|---|
| `autoswe_runs_total` | counter | `outcome` (done / failed / cancelled) |
| `autoswe_task_attempts` | histogram | — |
| `autoswe_first_pass_test_success_total` | counter | — |
| `autoswe_debug_success_total` | counter | — |
| `autoswe_tokens_per_solved_task` | histogram | — |
| `autoswe_cache_hit_rate` | histogram | — |
| `autoswe_sandbox_exec_seconds` | histogram | — |
| `autoswe_cost_usd_total` | counter | `role` |

### Two targets, not one

The API and the worker are separate processes with separate registries. Neither can see
the other's counters, so a scrape config needs both:

```yaml
scrape_configs:
  - job_name: autoswe-api
    static_configs: [{targets: ["api:8000"]}]     # /metrics
  - job_name: autoswe-worker
    static_configs: [{targets: ["worker:9100"]}]  # /metrics
```

The API's are about requests; the worker's are about runs. `METRICS_PORT=0` turns the
worker's endpoint off, which is what a second worker on the same host needs.

Both endpoints are unauthenticated, like `/healthz`. A scraper is infrastructure and
cannot hold an API key, and that is only defensible while every label is a role or an
outcome — never a repository, a goal, or a customer. There is a test asserting exactly
that set.

### The queries worth having

**Cost per solved task**, which is the metric — not cost per run. A run that spent half as
much and finished one task instead of three cost more per unit of work, and a total alone
hides that completely:

```promql
rate(autoswe_cost_usd_total[1d]) / rate(autoswe_first_pass_test_success_total[1d])
histogram_quantile(0.5, rate(autoswe_tokens_per_solved_task_bucket[1d]))
```

**Is the debug loop covering for a worse Coder?** A rise in the second without a rise in
the first reads as success on any dashboard that only counts finished tasks:

```promql
rate(autoswe_first_pass_test_success_total[1d])
rate(autoswe_debug_success_total[1d])
```

**Is caching working?** Below 0.5 after the first step of a run, something in the prefix is
moving — see `gateway/caching.py`:

```promql
histogram_quantile(0.5, rate(autoswe_cache_hit_rate_bucket[1h]))
```

### Live spend

`runs.cost_usd` is incremented on every model call, so `GET /runs/{id}` and the console
move during a long step rather than jumping at each step boundary. It is an increment
(`cost_usd = cost_usd + delta`) rather than a `SUM` over `llm_calls`, because it runs once
per call; `llm_calls` remains the source of truth and every step boundary reconciles
against it.
