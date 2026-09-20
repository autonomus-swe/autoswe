# Phase 5 — Scale and Cost (README milestone M5)

> Outcome: the system works on large repositories and stays cheap. A ranked repo map from a tree-sitter symbol index, semantic retrieval in pgvector, prompt caching with measured hit rates, context editing and compaction for long agent sessions, task budgets, budget-aware model routing, full tracing and metrics, hardened per-stack sandboxes. Demo line: "Run on a 3,000-file repo under $5."

Suggested duration: 6–8 days. Steps 5.1–5.3 (repository intelligence), 5.4–5.6 (LLM cost), and 5.7–5.9 (operations) are three independent tracks; do them in any order but finish 5.10 last.

---

## 0. Where this phase sits

**Prerequisites**
- Phase 4 exit checklist ticked (`v0.4.0`). The README §8 walkthrough produces a complete PR.
- A large open-source Python repository chosen for the scale test (2 000–5 000 files; something like `httpx` or `pydantic` sized), forked to your account.

**Delivers**
- `repo/symbols.py` (tree-sitter index), `tools/symbols.py` (`list_symbols`), migration `0003_repo_index` (`repo_symbols`, `repo_embeddings`)
- `repo/repomap.py` v2 (centrality + BM25 ranking, token-budgeted rendering, Redis cache)
- `repo/embeddings.py` (pgvector), `search_code(semantic=True)`
- `gateway/caching.py` (breakpoint placement), cache metrics
- `gateway/anthropic_provider.py`: context editing, compaction, task budgets, streaming deltas
- `gateway/routing.py` v2 (budget-aware downgrade), `gateway/budget.py` v3
- `observability/tracing.py` (OTLP + Langfuse), `observability/metrics.py` (Prometheus), worker metrics endpoint
- Parallel execution of parallel-safe tool calls
- `sandbox/images/{node,go}/Dockerfile`, egress proxy service, gVisor flag, GC job, per-profile limits
- `evals/results/m5.jsonl` with the scale numbers

**Deliberately deferred**
- Open-source model provider, MCP, SWE-bench (Phase 6).

---

## 1. Exit criteria

Audited by agents instructed to *disprove* each one. `docs/numbers.md` carries the
evidence; the verdicts are recorded here rather than the boxes being ticked.

- [x] Indexing a 3 000-file repository takes under 60 s on the worker; re-running on the same SHA is a no-op.
      Measured rather than extrapolated: django, **7 120 files and 44 292 symbols in
      9.9 s** — twice the size asked for, six times inside the budget. The no-op half is
      tested by a case that drives `index_repo` twice and fails when the reuse check is
      removed; the test previously named for it only exercised the storage predicate.
      Still parse-only and host-side, which `docs/numbers.md` states.
- [x] The repo map ranks the named function's file first; the rendered map is under 4 000 tokens for the scale repo.
      Met **after a correction**. `CHARS_PER_TOKEN` was 4 — the prose figure — and code
      measures 2.69–2.90, so a map "inside its 3 500-token budget" was really 5 193 tokens
      and this criterion was being reported as met while missed by a third.
- [x] `search_code(semantic=True, …)` returns the right file on the fixture auth service.
      `tests/fixtures/auth_service/` exists and is built so only meaning can answer the
      criterion's own query: `app/auth/bucket.py` is a token bucket containing neither
      "rate" nor "limit", asserted by a test so it cannot drift into a keyword match. Run
      against a real embedding model (`nomic-embed-text` via Ollama), with the hash
      provider as a control — it scores 0.0 against every chunk, because the query shares
      no word with the repository.

      Integration rather than e2e, deliberately: the criterion is about the index, and an
      end-to-end run would add a chat model and the quota problem below without testing
      anything more about search.
- [ ] Cache read tokens from the second turn; run-level cache hit rate above 60 %.
      `tests/e2e/test_m5_cache.py` exists and has never run: free tier, 50 requests/day.
- [ ] A Coder session that exceeds the context trigger completes with compaction blocks preserved (`provider.compacted`).
      Not achievable as written: no Anthropic provider, no compaction code, and
      `provider.compacted` is logged nowhere. `gateway/context.py` serves the same intent
      host-side and logs `cleared_tool_results`. Moved to Phase 6 with the provider.
- [~] At 90 % of the dollar budget, three roles route to Sonnet 5 and `budget_warning` carries `{"downgraded": […]}`.
      The table is correct and tested, including the `decompose`/`decomposer` name trap the
      document's own example contains. But **no Sonnet 5 is reachable in this build**, so
      on any single-model deployment the downgrade is real policy with no effect — which
      `_downgraded_roles` reports honestly as an empty list.
- [x] One OpenTelemetry trace per run with phase, step, llm_call, tool_call and sandbox_exec spans; `/metrics` exposes the §4.10 metrics.
      All five spans exist and nest; all seven metrics named in ARCHITECTURE §4.10 are
      present and served.
- [x] Node and Go fixture repositories run end to end in their images; installs go through the egress proxy and are denied for a non-allow-listed host.
      Both halves met. `repo/stacks.py` makes the install, the test command and the report
      format properties of the toolchain rather than assumptions about Python, and
      `tests/integration/test_stacks.py` drives a real Node and a real Go fixture through
      a real image — install, test, parse — and then breaks the *source* of each and
      asserts the failure comes back with a file and a line the Debugger can act on.
- [ ] Scale run: PR opened on the 3 000-file repo, total cost under $5, numbers recorded.
      The host-side numbers are recorded. The run needs quota.
- [ ] Tag `v0.5.0`.

**Six of ten met; the rest need money or a provider.** What is left:

- **Two need a chat model with quota** — the run-level cache hit rate and the scale run.
  $10 of OpenRouter credit raises the free-model allowance from 50 to 1 000 requests a day
  and is the whole dependency. The tests are written; `evals/results/` takes a row per run.
- **One needs the Anthropic provider**, deferred to Phase 6 — compaction cannot be tested
  against a provider that does not exist.
- **One is partial by its own wording**: the downgrade table is correct and tested, but no
  second model tier is reachable in this build, so the policy is real and its effect is
  nil. That resolves itself the moment a deployment configures `LLM_MODEL_SONNET`.

Nothing remains that is unfinished work.


---

## 2. Architecture slice

```
 SETUP ─► clone ─► tree-sitter index (repo_symbols, by SHA) ─► embeddings (background) ─► pgvector
                          │
 PLAN / DECOMPOSE ◄── repo map v2: PageRank(import + reference graph) × BM25(goal) → outline ≤ 4K tokens (Redis cached)
                          │
 CODER / DEBUGGER ◄── request = [tools] [system: role prompt ⟂cache] [system: profile + map ⟂cache] [messages…]
                       + context_management (clear old tool results) + compaction + task_budget
                          │
 gateway/budget ── llm_calls ──► cost, cache hit rate ──► routing v2 (downgrade at 90 %) ──► metrics / traces
```

---

## 3. Work breakdown

### Step 5.1 — tree-sitter symbol index and `list_symbols`

**Files:** `repo/symbols.py`, `repo/languages/{python,javascript,typescript,go}.py` (queries), `tools/symbols.py`, `storage/migrations/versions/0003_repo_index.py`, `tests/unit/test_symbols.py`.

- Dependencies: `tree-sitter` and `tree-sitter-language-pack` (grammars for all four languages in one wheel).
- Per language, two tree-sitter queries: **definitions** (module-level functions, classes, methods, Go funcs and types, JS/TS functions, classes, exported consts) capturing name, parameters, and node range; **references** (call expressions and attribute accesses) capturing the identifier. Signatures are the first line of the definition, trimmed to 120 characters.
- `index_repo(worktree, repo_sha) -> IndexStats`: walk source files (same exclusions as the repo map), parse with a per-language parser, and bulk-insert `repo_symbols(repo_sha, path, kind, name, signature, start_line, end_line, refs text[])`. Skip entirely if `select 1 from repo_symbols where repo_sha = $1 limit 1` exists. Parse files in a `ProcessPoolExecutor` (tree-sitter releases the GIL poorly across grammars; processes are simpler).
- `setup_node` calls `index_repo` after the clone, before the sandbox starts; it is host-side and needs no container.
- `list_symbols(path)` tool (read-only, parallel-safe) reads from the table for the run's `base_sha` and merges with a live parse of the file if it changed in the worktree (the Coder edits files; the index is by base SHA).

**Tests:** fixture files per language with known definitions and references; idempotent second index; a syntax-error file yields zero symbols and a warning, not an exception.

---

### Step 5.2 — Repo map v2: ranking and rendering

**Files:** `repo/repomap.py`, `repo/graph.py`, `tests/unit/test_repomap.py`.

- **Graph.** Nodes are files. Edges: import statements resolved to paths (Python `import a.b` / `from a import b` → `a/b.py` or `a/b/__init__.py`; JS/TS relative imports; Go package paths within the module), plus an edge from file B to file A for every reference in B to a symbol defined in A (name match, ties broken by import proximity). `networkx.pagerank` with `alpha=0.85`.
- **Lexical relevance.** `rank_bm25.BM25Okapi` over per-file documents made of symbol names split on case and underscores, plus path tokens. Query = goal tokens plus, when a plan exists, its `affected_files` basenames (used by `DECOMPOSE` and the Coder context).
- **Score.** `0.6 * pagerank_norm + 0.4 * bm25_norm`; manifests and files in `plan.affected_files` are pinned to the top; test files get a 0.7 multiplier unless the goal mentions tests.
- **Render.** For each file in rank order: path, then one line per definition (`def name(params) -> ret  [L12–40]`, `class Name  [L50–120]`, methods indented). Stop when the token budget is reached (default 3 500; estimate 4 characters per token, calibrated once with `client.messages.count_tokens` on a sample map and stored as a constant). Files that did not fit are listed by path only under "other files (N)".
- **Cache.** Redis `repomap:{sha}:{sha1(goal)}` for 24 h; also store as an artifact for the run.
- The v1 tree renderer remains the fallback when the index is missing (unsupported language).

**Tests:** on the fixture auth service, goal "fix token expiry check" ranks `app/auth/jwt.py` first; pinned files always appear; token budget honoured (±10 %).

---

### Step 5.3 — pgvector embeddings and semantic search

**Files:** `repo/embeddings.py`, `tools/search.py` (`semantic` mode), `tests/integration/test_embeddings.py`.

- Chunk by symbol: the definition's source (up to 60 lines; longer bodies split at 60-line boundaries with the signature repeated as a header). Text = `path · signature · body`.
- Embedding provider behind `EmbeddingProvider.embed(texts) -> list[list[float]]`: `VoyageProvider` (`voyage-code-3`, 1024 dims, batched 128 texts) or `LocalProvider` (`sentence-transformers`, any 1024-dim code model; slower, free, offline). Selected by `EMBEDDING_PROVIDER`.
- Table `repo_embeddings(repo_sha, path, chunk_id, embedding vector(1024), text)` with an HNSW index (`vector_cosine_ops`, `m=16`, `ef_construction=64`) in migration `0003`.
- `index_embeddings(worktree, repo_sha)` runs as a background arq job enqueued by `setup_node`; `ANALYZE` and `PLAN` do not wait for it. `search_code(semantic=True, query, k=10)` returns `path:line` plus the chunk's first line; if the index is not ready it falls back to `rg` on query keywords and says so in the result.
- Hybrid mode: when both `pattern` and `semantic=True` are given, union the top results and order by cosine score.

**Tests:** with a fake embedding provider (hash-based deterministic vectors) the nearest chunk for a query equals the planted one; fallback message when the index is absent.

---

### Step 5.4 — Prompt caching breakpoints and cache metrics

**Files:** `gateway/caching.py`, `gateway/anthropic_provider.py`, `observability/metrics.py`, `tests/unit/test_caching.py`, `tests/e2e/test_m5_cache.py`.

Request assembly is now explicit and identical for every role:

```
tools:    registry order for the role (fixed list; never reorder between calls)
system:   [ {text: <role prompt, static>,                        cache_control: ephemeral},
            {text: <RepoProfile + repo map + conventions>,       cache_control: ephemeral} ]
messages: [ task, file contents, … volatile … ]
```

- `caching.build_system(role, run_block) -> list[dict]` produces the two blocks. The role prompt contains no dates, run ids, or counters. The run block is rendered from `RepoFacts`/`RepoProfile` with `sort_keys=True` and the cached repo map, so it is byte-identical across every step of a run.
- Long Coder/Debugger loops: in `after_tool` (the runner's result-modification hook) add `cache_control` to the most recent tool result every 8 turns so the growing transcript prefix is cached too. Keep to the 4-breakpoint limit: two in `system`, at most two moving ones in `messages`.
- Each `llm_calls` row already stores `cache_read_tokens` and `cache_write_tokens`; add the derived metric `cache_hit_rate = cache_read / (input + cache_read)` per call, per step, per run, and a Prometheus histogram.
- Silent-invalidator audit as a unit test: render the same request twice for the same run and assert the serialized prefix (tools + system) is byte-identical; assert no `datetime`, `uuid`, or `seq` substrings in the role prompt.

**Tests:** block order and identical-prefix test; e2e records `cache_hit_rate` per step on the fixture and asserts the second Coder turn reads cache.

---

### Step 5.5 — Context editing, compaction, task budgets, streaming

**Files:** `gateway/anthropic_provider.py`, `contracts/budget.py` (`task_budget_tokens`), `tests/unit/test_provider_betas.py`.

For `coder` and `debugger` roles only:
- `betas=["context-management-2025-06-27", "compact-2026-01-12", "task-budgets-2026-03-13"]`.
- `context_management={"edits": [{"type": "clear_tool_uses_20250919"}]}` — old tool results are cleared server-side before the model sees the transcript; configure the strategy's trigger and keep options per the current docs so the last few results stay visible. Do not clear tool inputs; the Debugger reads its own earlier commands.
- Compaction is server-side and automatic once enabled; the requirement on our side is that the runner appends `response.content` verbatim (compaction blocks included) to the mirrored history. The `ManualLoopRunner` already does this; verify the beta runner does, log `provider.compacted` when a compaction block appears.
- Task budgets: `output_config={"effort": route.effort, "task_budget": {"type": "tokens", "total": budget.task_budget_tokens[role]}}` with defaults Coder 80 000 and Debugger 60 000 (minimum accepted is 20 000). Always stream (`client.beta.messages.stream`) for these requests so large `max_tokens` does not hit HTTP timeouts; leave `remaining` unset — the server tracks the countdown.
- Streaming deltas: forward text deltas as `agent_text` events (batched every 250 ms) and summarized thinking with `thinking={"type": "adaptive", "display": "summarized"}` (Phase 2 emitted per-message text; this replaces it).
- These betas are Anthropic-only; the provider interface does not expose them, so Phase 6's OpenAI-compatible provider is unaffected.

**Tests:** the request builder includes the betas and budget for `coder` and not for `analyzer`; a synthetic response with a compaction block is appended unchanged.

---

### Step 5.6 — Budget-aware routing

**Files:** `gateway/routing.py`, `gateway/budget.py`, `orchestrator/hooks.py`, `tests/unit/test_routing.py`.

```python
DOWNGRADE = {"planner": "claude-sonnet-5", "decompose": "claude-sonnet-5", "review": "claude-sonnet-5"}
def route_for(role: str, state: RunState) -> Route:
    base = ROUTES[role]
    if state.budget.fraction_used(state.usage, state.elapsed_s())["usd"] >= state.budget.warn_at_fraction \
       and role in DOWNGRADE:
        return Route(model=DOWNGRADE[role], effort=base.effort)
    return base
```

- Coder and Debugger never downgrade (a cheaper coder that needs more attempts is not cheaper — README §4.7). Haiku roles are already the floor.
- The `budget_warning` event gains `{"downgraded": [roles]}` when the first downgrade happens.
- Switching model for a role starts a new cache namespace for that role; accepted and logged.
- Cost per completed task is the metric (`cost_usd / tasks_done`); it is written to `runs` at finish and shown in the PR footer.

---

### Step 5.7 — Tracing and metrics

**Files:** `observability/tracing.py`, `observability/metrics.py`, `observability/langfuse.py`, `api/routes/metrics.py`, `orchestrator/worker.py` (metrics server on `:9100`), `docker-compose.yml` (langfuse, optional), `docs/observability.md`.

- OpenTelemetry SDK with OTLP exporter when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. One root span per run (`run`, attributes `run_id`, `repo`, `provider`); child spans `phase.<name>`, `step.<role>` (model, effort, tokens, cost, cache hit rate), `llm_call`, `tool.<name>` (exit code, duration), `sandbox.exec` (timeout, truncated). All of these call sites already exist from Phase 0's `trace_span`; this step only configures export and adds attributes.
- Langfuse: send generations from `on_message` with prompt, completion, usage, and cost so prompts are browsable; or point the OTLP exporter at Langfuse's OTLP endpoint and skip the SDK. Either is fine; pick one and document it.
- Prometheus (`prometheus-client`): `autoswe_runs_total{outcome}`, `autoswe_task_attempts` (histogram), `autoswe_first_pass_test_success_total`, `autoswe_debug_success_total`, `autoswe_tokens_per_solved_task` (histogram), `autoswe_cache_hit_rate` (histogram), `autoswe_sandbox_exec_seconds` (histogram), `autoswe_cost_usd_total{role}`. API exposes `/metrics`; the worker exposes `:9100/metrics`.
- `runs.cost_usd` is updated in `on_message` so `GET /runs/{id}` and the UI show live spend.

---

### Step 5.8 — Parallel tool execution

**Files:** `gateway/anthropic_provider.py`, `tests/unit/test_parallel_tools.py`.

When a yielded assistant message contains several `tool_use` blocks and every one of them is `parallel_safe`, execute them with `asyncio.gather` and return all `tool_result` blocks in a single user message (splitting them across messages teaches the model to stop parallelizing). With the beta runner, do this by intercepting the yielded message and appending the results through the runner's message hooks before it executes them itself; with `ManualLoopRunner` it is the default. If any block is mutating, execute all of them serially in order. Failed tools return `is_error: true` results, never dropped.

**Tests:** three `search_code` calls run concurrently (timing assertion with a slow fake); one `bash` among them forces serial order.

---

### Step 5.9 — Sandbox hardening and per-stack images

**Files:** `sandbox/images/node/Dockerfile`, `sandbox/images/go/Dockerfile`, `sandbox/docker.py`, `sandbox/select.py`, `proxy/` (egress proxy image), `docker-compose.yml`, `orchestrator/gc.py`, `tests/integration/test_egress.py`.

- **Images.** `node-20` (node, npm, pnpm, ripgrep, git) and `go-1.23` (go, ripgrep, git, `gotestsum` for JUnit). `sandbox/select.py: image_for(facts: RepoFacts) -> str` picks by primary language; unknown → python image with a warning (bash still works).
- **Egress proxy.** A small container (`tinyproxy` or `squid`) that allows `CONNECT` only to the allow-list (`pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`, `proxy.golang.org`, `sum.golang.org`, `github.com` for Go modules if needed). `agent-install` becomes `internal: true`; the proxy is attached to both `agent-install` and the default network. The install step runs with `HTTP_PROXY`/`HTTPS_PROXY` set to the proxy; after install the container is disconnected as before. Package managers respect the proxy variables (`uv`, `pip`, `npm`, `go` all do).
- **gVisor.** `SANDBOX_RUNTIME=runsc` passes `runtime="runsc"`; document the host install (`runsc` binary + Docker daemon config). Keep it optional; make it default in the `prod` compose profile.
- **Limits per profile.** `file_count > 2000` → `mem_limit=6g`; Go and Node builds → `nano_cpus=4e9` during install only (recreate is not possible, so set the higher value at creation when the profile predicts it).
- **GC.** An arq cron job every 10 min removes containers labelled `autoswe.run_id` whose run is terminal and older than `KEEP_FAILED_SANDBOX_TTL`, worktrees of terminal runs older than 1 h, and bare clones untouched for 14 days.
  **Measured 2026-09-16, after Phase 3:** 41 worktrees, **322 MB**. `teardown` removes a worktree only when the run pushed (`if res.worktree is not None and state.pushed`), which is deliberate — a failed run's tree is the evidence — but nothing then reaps it, so every failed or cancelled run leaves one behind with its `.venv` inside. At roughly 8 MB a run this is the first thing in this phase that a developer notices, and the reaper above is the fix.

**Tests:** in the sandbox during install, `curl https://pypi.org` succeeds via the proxy and `curl https://example.com` is refused (403 from the proxy); after disconnect both fail; Node and Go fixture repos complete the M1 flow.

---

### Step 5.10 — The scale run

**Files:** `evals/scale.py`, `evals/results/m5.jsonl`, `docs/numbers.md`.

Procedure on the forked large repo:
1. Goal that touches two or three files and adds a test (for example, "add a `__repr__` to `X` and a test for it"; keep it realistic and small).
2. Run with default budget (`$10`). Record: index time, embedding time, repo map tokens, tasks, attempts, review rounds, per-role cost, cache hit rate, wall-clock, total cost.
3. Ablation: run once more with `repomap=off` (Coder and Planner get the v1 tree only). Record the same numbers.
4. Write `docs/numbers.md` with the table; this feeds README §14 and §15.

Target: PR opened, total under $5, cache hit rate above 60 %. If cost is above target, the usual culprits in order: a cold cache (check `cache_write` vs `cache_read`), too many fenced files in the Coder context, a review pre-pass on an oversized diff, or the Debugger looping on an environment failure.

---

## 4. Testing plan

| Tier | Coverage | Command |
|---|---|---|
| unit | symbol queries per language, graph + ranking, rendering budget, caching prefix identity, routing downgrade, provider betas, parallel execution | `make test` |
| integration | index idempotency with timing, pgvector search, egress proxy allow/deny, GC job, metrics endpoints | `make test-int` |
| e2e | cache hit rate on the fixture, semantic search on the auth fixture, Node and Go fixtures, the scale run | `uv run pytest -m e2e tests/e2e/test_m5*.py` |

---

## 5. Demo script

```bash
make sandbox-images                      # python, node, go
docker compose --profile observability up -d langfuse
uv run autoswe run --repo https://github.com/<you>/<large-python-repo> \
  --goal "Add a __repr__ to <Class> in <module> showing its public fields, with a unit test."
uv run autoswe watch <id>
# setup: indexed 3 112 files, 41 208 symbols in 38 s · repo map 3 420 tokens
# … step coder t2: cache_hit_rate 0.81 · pr_opened
uv run autoswe status <id> --json | jq '{cost_usd, tasks_done, cache_hit_rate, wall_clock_s}'
# open Langfuse: one trace, spans per phase/step/tool; open /metrics
```

---

## 6. Pitfalls

- **tree-sitter grammar versions.** Pin `tree-sitter` and `tree-sitter-language-pack` together; query syntax differs across grammar versions, and a query that fails to compile returns nothing silently unless you assert on it.
- **Cache invalidation by accident.** A single changed byte in the tools list or the role prompt discards the whole prefix. The identical-prefix test is cheap; keep it in CI. Watch for JSON key ordering and for the repo map being re-rendered with a different token budget mid-run.
- **Compaction and history mirroring.** If you ever rebuild the message list from your own copy, keep every compaction block. Losing one makes the next request re-send the full history and the run silently gets expensive.
- **Task budget is advisory.** It paces the model; `max_tokens` is the hard cap. Keep `max_tokens` well above the budget or the model gets cut off before it can wrap up.
- **Proxy for HTTPS.** Package managers use `CONNECT`; the allow-list must be on hostnames of the tunnel, not on URLs. Test with `curl -v` through the proxy before wiring it in.
- **gVisor and bind mounts.** `runsc` needs `--file-access=shared` (or the equivalent daemon flag) for the worktree mount to reflect host-side edits immediately; without it, the Coder's edits made on the host are not visible in the container until a remount.
- **Background embedding jobs and teardown.** If the run finishes before embeddings are done, the job must not fail on a removed worktree; it reads from the bare clone at `base_sha` instead.

---

## 7. Checklist before Phase 6

- [ ] Exit criteria in §1 all ticked; `docs/numbers.md` filled with the scale and ablation numbers.
- [ ] The provider interface has not grown Anthropic-specific parameters; Phase 6's provider must implement `parse()` and `run_tools()` only.
- [ ] You can explain the breakpoint placement, why coder/debugger never downgrade, and how the repo map score is computed.
- [ ] Tag `v0.5.0`.
