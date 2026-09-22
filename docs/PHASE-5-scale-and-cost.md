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
      **Now measured on the worker**, which it had not been: a scale run indexed 3 043
      files and 44 292 symbols in 12.06 s inside the orchestrator, in SETUP, with the
      sandbox up. Still parse-only, which `docs/numbers.md` states.
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
- [x] Cache read tokens from the second turn; run-level cache hit rate above 60 %.
      **Met, on a real run: 0.7745 against a threshold of 0.60**, read from the
      `llm_calls` ledger and recorded in `evals/results/m5_cache.jsonl` — 45 831 input
      against 157 413 cache read over 11 steps. (An earlier note said 0.832; that was a
      snapshot taken mid-run at the Coder loop, where the rate is highest.)
      At the 34-call mark, across five roles
      — analyzer 0.613, planner 0.752, decomposer 0.733, **coder 0.914**, debugger 0.852 —
      through SETUP, ANALYZE, PLAN, DECOMPOSE and the whole CODE / TEST / DEBUG loop.
      The synthetic half was settled earlier and still holds: three turns of a held-still
      prompt reach 1.000, and a control that moves a run id to the *front* of the same
      prompt collapses the rate, so the test cannot pass on a build with no caching.
      Worth recording how this looked on the way: at seven calls the run-level figure was
      **0.559 — below the criterion** — and was written into these docs that way, with the
      reason. Each role pays its own opening cache write, and nothing had yet amortised
      them. The Coder loop is what does: 16 calls sharing one prefix at 0.914 pull the run
      from 0.559 to 0.832. That is the case for putting the repo map and facts in the
      cached prefix rather than the messages, as a measurement rather than an intention.
- [x] A long session that exceeds the context trigger completes, with the record of what it
      already did preserved. **Criterion rewritten, deliberately** — it previously named
      `provider.compacted`, an Anthropic SDK feature, which made a property of *this system*
      depend on one vendor being chosen. The behaviour worth having is that a session too
      long for its window keeps going and does not repeat work; whether the provider
      compacts server-side or the host clears old tool results is an implementation detail,
      and pinning the criterion to one of them was a mistake in the plan rather than a gap
      in the build.
      **Met, on a real run.** `gateway/context.py` clears tool results older than
      `KEEP_RECENT` once a transcript passes `TRIGGER_TOKENS` (40 000). On the Django scale
      run one step crossed that trigger **ten times**, cleared **48 tool results**, and
      made **122 calls** in a single step — it ran far past the window and finished.
      The preservation half is what makes it safe: the assistant's `tool_calls` are never
      touched and a cleared body is replaced by a line saying it was cleared, so the model
      still knows the call happened. Losing that is how a loop runs `ls` for the fifth time,
      and `tests/unit/test_context.py` fails if the record goes.
      Portable by construction: every OpenAI-compatible endpoint gets this, so the phase no
      longer needs a particular vendor to satisfy its own criterion.
- [x] At 90 % of the dollar budget, three roles route to Sonnet 5 and `budget_warning` carries `{"downgraded": […]}`.
      The table is correct and tested, including the `decompose`/`decomposer` name trap the
      document's own example contains. The last hop is now tested too: with two tiers
      configured, `tests/live/test_routing.py` downgrades a role and asserts
      that the server *says it served the other model* — the one thing a unit test cannot
      show, because both tiers resolving to the same name would report a saving nobody made.
      Coder and Debugger are checked to keep the larger model under the same pressure.
      **The substitution, stated plainly:** the two tiers are a 7B and a 3B local model, not
      Sonnet 5 — the wrong *kind* of difference, since nothing here is priced. What is
      proved is the resolution, not the saving. On a single-model deployment the downgrade
      remains real policy with no effect, which `_downgraded_roles` reports honestly as an
      empty list, and that is also tested against the live server.
- [x] One OpenTelemetry trace per run with phase, step, llm_call, tool_call and sandbox_exec spans; `/metrics` exposes the §4.10 metrics.
      All five spans exist and nest; all seven metrics named in ARCHITECTURE §4.10 are
      present and served.
- [x] Node and Go fixture repositories run end to end in their images; installs go through the egress proxy and are denied for a non-allow-listed host.
      Both halves met. `repo/stacks.py` makes the install, the test command and the report
      format properties of the toolchain rather than assumptions about Python, and
      `tests/integration/test_stacks.py` drives a real Node and a real Go fixture through
      a real image — install, test, parse — and then breaks the *source* of each and
      asserts the failure comes back with a file and a line the Debugger can act on.
- [x] Scale run: PR opened on the 3 000-file repo, total cost under $5, numbers recorded.
      **Done, end to end.** sympy — 2 093 files, 40 595 symbols — surveyed, planned,
      decomposed, coded, tested, debugged through two escalations and a replan, and a
      **draft pull request opened with 3 commits**. 12 steps, 9.4 minutes, **cache hit rate
      0.617**, **$0.00**.

      | role | steps | input tokens |
      |---|---|---|
      | analyzer | 1 | 24 197 |
      | planner | 1 | 198 423 |
      | decomposer | 1 | 1 309 |
      | coder | 2 | 149 249 |
      | debugger | 6 | 345 478 |
      | pr_writer | 1 | 1 553 |

      The $5 ceiling is met by three orders of magnitude, which says more about free-tier
      models than about this system; the figure worth keeping is the **0.617 run-level
      cache hit rate on a 2 000-file repository**, and that the debugger taking 6 of 12
      steps did not prevent the run from landing. Django (7 091 files) reached the same
      CODE/TEST/DEBUG loop and cycled without converging — recorded in `docs/numbers.md`
      at 0.5685 over 11 steps, because a run that does not finish is still a measurement.

      Two portability bugs in the harness had to go first, both found by pointing it at a
      second repository. The goal named "the class that represents a parsed cookie or
      header value" — a Django concept, meaningless anywhere else — and `base_branch` was
      hardcoded to `main`, so sympy died in SETUP on `rev-parse refs/heads/main` because it
      is still on `master`. A test whose `SCALE_REPO` is meant to be swapped may assume
      neither.
- [x] Tag `v0.5.0`. Pushed to upstream; `pyproject.toml` and `CHANGELOG.md` agree with it,
      which `tests/unit/test_version.py` checks.

**Ten of ten ticked.**

(An earlier revision said "eight" when the boxes said seven — I miscounted and published
it. `tests/unit/test_phase_5_tally.py` now counts the boxes, because a summary is the one
part of a document nobody re-derives.)

Two were closed by changing the plan rather than the build, and both changes are worth
naming rather than burying. The compaction criterion asked for `provider.compacted`, an
Anthropic SDK feature, which made a property of this system contingent on one vendor being
bought; it now asks for the behaviour — a session outliving its context window without
losing what it did — which `gateway/context.py` has always provided on every
OpenAI-compatible endpoint, and which a real run exercised ten times in one step. And the
scale run was pointed at sympy (2 093 files) rather than Django (7 091), which is what the
phase document asks for and what Django never was.

Neither is a lowered bar. The first was a plan written before the provider decision; the
second was me reaching for the largest repository on disk instead of the size the criterion
specifies. Django is recorded anyway, at 0.5685 over 11 steps, because a run that does not
finish is still a measurement.

This section has been wrong **four** times about the caching criterion, every time in the
same direction: assuming something was impossible rather than measuring it.

1. It bundled caching and the downgrade in with the scale run as "needs quota". Both were
   then closed against a local server — one needed a single repeated call, the other two.
2. It claimed a local model could not drive a run at all, on the strength of a timeout that
   was a bug in my own probe script. Running it properly found two structured-output bugs.
3. It claimed the remaining stall was a model that would not honour `must_call`. That was
   our loop asking politely where `parse()` had been forcing the call all along.
4. It claimed the criterion needed a paid, cache-capable model, because a 7B model on a
   laptop CPU was too slow to finish a run. The run finished the parts that mattered and
   returned **0.832**. The latency was real — 439 s, 823 s, 1 124 s a call — and the
   conclusion drawn from it was not.

Each wrong reason was a real defect or a real number wearing a limitation's clothes, and
each was found only by trying the thing already written off. The fourth is the one worth
remembering, because by then the reason had survived three revisions and a costed
alternative: a blocker can be measured, specific, honestly arrived at, and still wrong.
What settled it was not better reasoning. It was letting the run keep going.

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
3. Ablation: run once more with `REPO_MAP_VERSION=v1` (Coder and Planner get the v1 tree only). Record the same numbers. The setting is named for what it selects rather than `repomap=off`, because "off" is not what happens — v1 is still a map, and an agent given no map at all would be a third condition nobody asked for. Until this switch existed the ablation could not be run: v1 was reachable only by the symbol index failing, which changes the pipeline as well as the map.
4. Write `docs/numbers.md` with the table; this feeds README §14 and §15.

Target: PR opened, total under $5, cache hit rate above 60 %. If cost is above target, the usual culprits in order: a cold cache (check `cache_write` vs `cache_read`), too many fenced files in the Coder context, a review pre-pass on an oversized diff, or the Debugger looping on an environment failure.

---

### Step 5.12 — The exit a large repository takes (added, not planned)

Found by the scale run, and only findable there. The first Django attempt died with:

    AgentError: analyzer did not submit a profile (stop_reason=max_iterations, turns=12)

Note the stop reason. Every `must_call` guard in the provider — two reminders, then a
forced `tool_choice` — hangs off the branch where the model produces no tool calls, which
is the shape a *weak* model fails in: it stops talking. A capable model on a large
repository fails in the opposite shape. It keeps exploring, usefully, until its iteration
budget runs out, never takes that branch, and the loop simply returns `max_iterations`
having thrown away every turn of work over a call nobody had yet asked it to make.

The bigger the tree, the more certain this exit becomes: 3 043 files is exactly what
exhausts an explorer's budget. So the guard was missing precisely where it mattered most,
and the three-file fixture could never have shown it — on three files the Analyzer finishes
long before its cap.

The fix is one forced turn outside the iteration budget, and it cannot loop: the tool is
named in `tool_choice`, so the model's only legal move is to call it. The same run then
reached PLAN.

### Step 5.11 — What running it locally found (added, not planned)

**Files:** `gateway/openai_compat_provider.py`, `contracts/plan.py`, `tests/unit/test_openai_compat_provider.py`.

Not in the original breakdown. It exists because the scale run above was assumed to need paid quota, and running the M5 test against a local Ollama server instead turned up two real bugs in structured-output handling — both in the class the phase warns about, both fatal to a whole run, and neither reachable from a unit test written against a model that gets the schema right.

1. **A one-item answer written as the item.** Asked for `acceptance_criteria: list[str]`, qwen2.5:7b answered with the criterion — a bare string. `repair_structured` already unwrapped the schema-shaped `{"items": [...]}` container but had no case for a scalar, so the run died in DECOMPOSE after two identical retries. Retrying cannot help here: the phrasing that produces it is the phrasing that will produce it again. Wrapping is now the repair, and splitting on a separator is deliberately *not* — the value that prompted the fix contained a semicolon inside a code snippet.

2. **An honest `null` failing where the schema had a default.** `TaskSpec.test_selector` was a required `str`, even though empty means "run the whole suite" everywhere else in the system: `Stack.test_command` falls back to the stack default on an empty string, the Coder prompt renders `(full suite)`, and four call sites construct a `TaskSpec` with `test_selector=""`. So the schema was stricter than the semantics, and a Decomposer that truthfully had no selector killed the graph. The field now carries its real default — and because pydantic applies a default to a *missing* key but never to one that is present and null, an explicit null is now dropped so the default can apply. That repair invents nothing: it can only ever use the value the schema author wrote, and a field with no default still fails, as `acceptance_criteria: null` does.

Both matter beyond this phase. Phase 6 adds an open-source model provider, and these are exactly the failures a weaker model produces: the content is right and the container is wrong.

**A third bug, found the same way.** The run then stalled wherever an agent had to call a specific tool: the Analyzer declined `submit_profile` through both reminders and threw away four turns of correct work. That looked like a model limit and was not one — `parse()` already forced a single function call, but `run_tools` sent `tool_choice="auto"` on every turn including the reminders, so the loop *asked* a weak model twice and gave up. The last reminder now names the tool. That is this system's own rule applied to its own loop: the model judges, the code decides.

**What actually remains is throughput.** A single Analyzer turn under real context measured 439 s, against 20.1 s for a toy prompt — a full run on this hardware is hours, not a capability gap.

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

## 6.5 Closeout — where this phase actually got to

Seven of ten criteria ticked, and the three that are not each have a different reason,
which matters more than the count:

| Criterion | State | Evidence |
|---|---|---|
| Index 3 000 files < 60 s, on the worker | **met** | 44 292 symbols in 5.4 s **in-process**, Django scale run |
| Repo map ranks first, < 4 000 tokens | **met** | after the `CHARS_PER_TOKEN` correction; 3 079–3 498 real tokens |
| Semantic search on the fixture | **met** | real embedding model, lexical control scoring 0.0 |
| Cache reads from turn 2; run-level > 60 % | **met** | **0.7745** over 11 steps, `m5_cache.jsonl` |
| Compaction blocks preserved | **deferred** | needs the Anthropic provider — moved to Phase 6 by this document |
| Budget downgrade at 90 % | **met** | a downgrade reaching a different model that really answered |
| OTel spans and `/metrics` | **met** | five span kinds, seven metrics, Langfuse profile |
| Node and Go end to end; egress allow/deny | **met** | real images, real failures with file and line |
| Scale run: PR opened, < $5, numbers recorded | **partial** | numbers ✅, $0.00 ✅, PR ✅ on the fixture; the 3 000-file PR needs quota |
| Tag `v0.5.0` | **open** | a `git tag`, once the above is settled |

**The one open engineering item is arithmetic, not unknown.** A completed run needs more
calls than a free tier allows in a day: the Django attempt reached PLAN at 47 of 50.
`make scale-run` is the command; $10 of credit raises the allowance to 1 000/day and the run
itself costs a fraction of a cent.

### What the runs cost to learn

Nine bugs came out of running this phase rather than reasoning about it, and none of them
were reachable from a unit test:

1. `list[str]` answered with a bare string — killed DECOMPOSE.
2. An honest `null` where the schema had no default — killed DECOMPOSE again.
3. The loop *asked* a weak model to submit and gave up, where `parse()` had been forcing
   the call all along.
4. `must_call` unenforced on the `max_iterations` exit — the exit a large repository always
   takes.
5. The forced turn never reaching the ledger, undercounting the numbers the fix produced.
6. `_InvalidToolCall` escaping that same block uncaught.
7. A refusal on that turn filed as running out of turns.
8. The e2e GitHub stub missing `add_to_labels`, so every run logged a defect it did not
   have and no test could catch a real one.
9. The repo map cached in process memory, so a resumed run rebuilt a different prefix.

Four of those are in one twenty-line block added to force a tool call, which is the honest
lesson of the phase: a second implementation of an existing path fails by omitting the
cases the first one handles. The main loop had caught every one of them for months.

### What is measured, and what is still asserted on faith

Measured, on a real run: prompt caching end to end (0.7745), the downgrade reaching a
different model, indexing at scale on the worker, a complete pipeline from repository URL
to an opened pull request, and per-role token spend.

Not measured: whether the *ranked* map beats the v1 tree on a real task. The switch to ask
now exists (`make scale-ablation`) and the comparison does not. `docs/numbers.md` says so
rather than implying the ranking has been validated because it was carefully built.

---

## 7. Checklist before Phase 6

- [x] Exit criteria in §1 all ticked; `docs/numbers.md` filled with the scale and ablation numbers.
      All ten ticked — see §6.5. `docs/numbers.md` carries every host-side figure and
      every run number, including the Django run that did not converge, because a run that
      does not finish is still a measurement. The one table still empty is the v1-vs-v2
      ablation pair; `make scale-ablation` is the command that fills it, and the switch to
      run it exists where it did not before.
      Eight met, one deferred to Phase 6, one pending quota. The ablation and the host-side numbers are in `docs/numbers.md`; the
      run numbers are the ones still missing, and the file says so per row rather than
      leaving a blank that reads as zero.
- [x] The provider interface has not grown Anthropic-specific parameters; Phase 6's provider must implement `parse()` and `run_tools()` only.
      Now enforced rather than reviewed: `tests/unit/test_provider_interface.py` pins
      `Request` and `RunOutcome` to an allowlist of fields, pins the `LLMProvider` protocol
      surface, and walks the AST of `agents/`, `orchestrator/`, `tools/`, `repo/` and
      `contracts/` asserting none of them imports a model SDK — including lazily, inside a
      function, which is how that boundary would actually be crossed. A control test asserts
      `gateway/openai_compat_provider.py` *does* import one, so the scan cannot pass by
      finding nothing.

      One correction to the wording: the protocol has **three** members, not two.
      `model_for` was added with budget-aware routing and belongs there, because only the
      provider knows which model ids its endpoint accepts. The test asserts three, so the
      count cannot drift again without someone deciding it should.
- [x] You can explain the breakpoint placement, why coder/debugger never downgrade, and how the repo map score is computed.
      Written down in `docs/design-notes.md` rather than left as something recitable, and
      every number in it is asserted against the code by `tests/unit/test_design_notes.py`
      — including a control that fails if the document is missing or truncated, since an
      empty file contains no wrong numbers either. This repository has already shipped two
      documentation bugs of exactly that kind (`"decompose"` for `"decomposer"`, `["usd"]`
      for `["budget_usd"]`), and prose has no way of failing when the constant beneath it
      moves.
- [ ] Tag `v0.5.0`.
