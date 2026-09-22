# Phase 6 — Interop, Independence, and Evaluation (README milestone M6)

> Outcome: the platform is an MCP server other agents can drive and an MCP client that mounts external tools; it runs on an open-source model behind vLLM with no vendor dependency; it has a benchmark harness that produces the numbers for README §14 and §15; the CLI is finished and `v1.0.0` is tagged. Demo line: "Drive it from Claude Code over MCP; run offline on an open model."

Suggested duration: 6–8 days. Steps 6.1–6.2 (MCP), 6.3 (open-source provider), and 6.4 (evals) are independent tracks. 6.5–6.6 close the project.

---

## 0. Where this phase sits

**Prerequisites**
- Phase 5 exit checklist ticked (`v0.5.0`); `docs/numbers.md` has the scale numbers.
- For 6.3: a machine with a GPU (24 GB VRAM runs a quantized Qwen3-Coder-30B-A3B; a smaller Qwen3-Coder or DeepSeek-Coder variant fits on less) or a cloud GPU box for a day; Ollama on a laptop is acceptable for development.
- For 6.4: the `swebench` package and its harness Docker images (large; pull ahead of time).

**Delivers**
- `mcp_bridge/server.py` (FastMCP), `cli/mcp.py` (stdio entrypoint), HTTP mount at `/mcp`
- `mcp_bridge/client.py`, `mcp_servers.yaml`, role tool-set configuration
- `gateway/openai_compat_provider.py`, `gateway/manual_loop.py` (shared), `RunCreate.provider`
- `evals/tasks/*.yaml`, `evals/run.py`, `evals/swebench.py`, `evals/judge.py`, `evals/report.py`, `evals/results/`
- `cli/main.py` finished; `docs/` finished; `CHANGELOG.md`; tag `v1.0.0`

---

## 1. Exit criteria

Ticked only where something was run. A criterion whose proof is a person watching an editor
is recorded as such rather than ticked from the code that would make it work.

- [~] From Claude Code (`claude mcp add autoswe -- uv run autoswe-mcp`), the prompt "use autoswe to implement X in repo Y and tell me when the PR is up" creates a run, waits, and reports the PR URL.
      **The transport is proven; the demo is not yet run.** `autoswe-mcp` is spawned as a
      subprocess in `tests/integration/test_mcp_server.py` and drives `create_run` and
      `get_run` against a real Postgres and Redis, so the console script resolves, the
      process serves MCP on its pipes, and nothing corrupts stdout. What remains is a
      person in Claude Code with a worker running and a GitHub token — `docs/mcp.md` §2 is
      the script. Ticking this from passing tests would be claiming a result nobody saw.
- [x] The Analyzer can call a mounted read-only Postgres MCP tool, and a mutating GitHub MCP tool pauses the run for approval (integration tests with a stub MCP server).
      Both halves in `tests/integration/test_mcp_client.py`, against a stub MCP server
      running as a real subprocess. The approval half is asserted through
      `OrchestratorHooks.before_tool` rather than by reading `requires_approval` off the
      tool: a flag nothing consults is not a gate. The run parks, the event carries the
      tool's name and its arguments, and an approval lets it through while a rejection
      returns the reason and never reaches the server.
- [~] `POST /runs {provider: "openai_compat"}` completes the M1 fixture end to end on Qwen3-Coder via vLLM with no Anthropic call (`llm_calls.provider` all `openai_compat`).
      **Not ticked, and the reason is that ticking it would mean nothing here.** This
      build has no Anthropic provider and is not getting one, so "no Anthropic call" is
      true of every run that has ever happened — a criterion satisfied by the absence of
      a feature is not a result.

      What was missing and is now built: the field is accepted, validated against what
      this deployment can construct, and **honoured by the worker**, which builds from the
      run's row rather than from `LLM_PROVIDER`. `runs.provider` had been written and
      ignored since Phase 1, which made `llm_calls.provider` — the thing this criterion
      asks you to inspect — evidence of the process's configuration rather than of the
      run's. `tests/integration/test_provider_selection.py` covers it; a `vllm` service is
      in compose behind the `gpu` profile and `docs/open-source-model.md` is the guide.

      What remains is a GPU and a Qwen3-Coder run on it. Phase 5 already produced the
      equivalent evidence on free and local models — a complete run to a draft PR in
      9.4 minutes for $0.00, `docs/numbers.md` — so this is the same claim on larger
      hardware, not an unproven one.
- [~] `evals/run.py --suite private` runs the 20–30 task suite and writes per-task resolved/attempts/rounds/cost/cache-hit; `evals/report.py` renders the table.
      **The harness is built and tested; the suite is three tasks, not twenty.** Every
      field the criterion names is recorded, and `/runs/{id}/detail` was extended to carry
      the cache totals so the harness never has to reach past the API to get them.

      Three rather than twenty because the remaining twenty-seven would be tasks invented
      to fill a count, against fixtures written to make them passable. A suite whose size
      is its own justification measures the person who wrote it. The three that ship are
      calibration — one feature, one bugfix, one that the M1 end-to-end test already
      drives — and `docs/evals.md` §2 is how to add real ones against repositories you
      actually maintain.
- [~] `evals/swebench.py --limit 50` produces `predictions.jsonl` and the official harness score; the number is in README §14 and the X/Y in §15 are filled.
      `predictions.jsonl` is produced; the score is not, and should not be yet. Each
      instance pins a `base_commit` and `RunCreate` takes a branch, so every run would
      start from the branch head and a patch would fail to apply for a reason the score
      cannot show. `--require-sha` refuses to run rather than producing a number nobody
      can attribute. Extending `RunCreate` to accept a SHA is the blocker, and it is named
      in this plan's own step 6.4.
- [~] Ablations recorded: with/without Debugger, with/without repo map, Opus 5 vs Sonnet 5 as Coder, Claude vs Qwen3-Coder, effort `high` vs `xhigh`.
      `evals/report.compare` renders the table and `--results` names an arm, so the
      mechanism exists. Two of the five arms are runnable here — `REPO_MAP_VERSION=v1`
      and `max_debug_attempts=0`. The other three compare model tiers a single-model
      deployment cannot distinguish, and running them would produce two identical columns
      with different labels. `docs/evals.md` §5 says which is which.
- [ ] `autoswe --help` lists `run`, `watch`, `status`, `artifacts`, `answer`, `approve`, `reject`, `cancel`, `eval`; `--json` works on all read commands.
- [ ] Tag `v1.0.0`.

---

## 2. Architecture slice

```
 Claude Code / IDE / other agent ──MCP──► mcp_bridge/server (FastMCP) ──► control plane (same code as HTTP)
 agents ──Tool protocol──► mcp_bridge/client ──MCP──► GitHub MCP · Postgres MCP (read-only)
 RunCreate.provider = anthropic | openai_compat ──► gateway.{AnthropicProvider | OpenAICompatProvider(vLLM: Qwen3-Coder)}
 evals/run.py ──► real API ──► results/*.jsonl ──► report table ──► README §14/§15
```

---

## 3. Work breakdown

### Step 6.1 — MCP server

**Files:** `mcp_bridge/server.py`, `cli/mcp.py`, `api/main.py` (mount), `tests/integration/test_mcp_server.py`, `docs/mcp.md`.

Use `FastMCP` from the `mcp` SDK. Tools (each a thin call into the same service functions the HTTP routes use; no logic in the MCP layer):

| Tool | Input | Output |
|---|---|---|
| `create_run` | `repo_url, goal, base_branch="main", budget_usd=10, unattended=false, provider="anthropic"` | `{run_id}` |
| `get_run` | `run_id` | `RunSummary` |
| `wait_for_run` | `run_id, timeout_s=1800` | terminal `RunSummary` (polls every 10 s; returns early on `awaiting_input` with the question so the caller can `answer_run`) |
| `answer_run` / `approve_tool` / `reject_tool` / `cancel_run` | as HTTP | `{ok}` |
| `list_events` | `run_id, since_id="0-0", limit=200` | events |
| `get_artifact` | `run_id, kind` | content |
| `search_code` / `read_file` | `run_id, …` | as the tools, on that run's worktree (only while the run is active or kept) |

Resources: `run://{run_id}/artifacts/{kind}` for clients that prefer resources over tool calls.

Transports: stdio (`autoswe-mcp` console script; auth via `AUTOSWE_API_KEY` env, calling the local service functions directly, no HTTP hop) and streamable HTTP mounted at `/mcp` on the FastAPI app behind the same API-key dependency. Run the MCP Inspector against both during development.

**Tests:** in-process FastMCP client calls `create_run` with a fake queue and gets a run id; `wait_for_run` returns on `awaiting_input`.

---

### Step 6.2 — MCP client: external servers as tools

**Files:** `mcp_bridge/client.py`, `mcp_servers.yaml`, `tools/registry.py` (dynamic registration), `tests/integration/test_mcp_client.py` (with a stub FastMCP server started in-process).

```yaml
# mcp_servers.yaml
servers:
  github:
    transport: stdio
    command: ["npx", "-y", "@modelcontextprotocol/server-github"]
    env: { GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}" }     # resolved in the worker, never sent to a sandbox
    roles: [planner, analyzer, pr_writer]
    allow: [get_issue, list_issues, list_pull_requests, get_pull_request, add_issue_comment]
    mutating: [add_issue_comment]                                  # requires approval
  postgres:
    transport: stdio
    command: ["npx", "-y", "@modelcontextprotocol/server-postgres", "${TARGET_DATABASE_URL}"]
    roles: [analyzer]
    allow: [query]
    read_only: true
```

- `mount(server_cfg) -> list[Tool]`: open a `ClientSession` over the configured transport, `list_tools()`, and wrap each allow-listed tool as a `Tool` whose `input_schema` is the server's schema, `mutating = name in cfg.mutating`, `parallel_safe = not mutating`, `requires_approval = mutating`. Names are prefixed `mcp_<server>_<tool>` to avoid collisions. Calls go through `session.call_tool` with a 60 s timeout; errors map to `ToolResult(is_error=True)`.
- Sessions are opened once per worker process and shared across runs; reconnect on failure.
- The read-only-role assertion in the registry still applies: mounting a mutating MCP tool into `analyzer` fails at startup.
- Alternative paths, documented but not default: the Anthropic SDK's `anthropic.lib.tools.mcp` conversion helpers when using the beta tool runner directly, and the API-native MCP connector (`mcp_servers` + `mcp_toolset` with beta `mcp-client-2025-11-20`) for URL-reachable servers. The bridged client stays the default because policy flags and approvals are enforced in the harness.

**Tests:** the stub server exposes `echo` (read-only) and `write` (mutating); `echo` runs; `write` pauses for approval; a tool not in `allow` is not registered.

---

### Step 6.3 — OpenAI-compatible provider (vLLM + Qwen3-Coder)

**Files:** `gateway/openai_compat_provider.py`, `gateway/manual_loop.py`, `gateway/tool_schemas.py`, `api/schemas.py` (`RunCreate.provider`), `docker-compose.yml` (`vllm` service), `tests/unit/test_openai_compat_provider.py`, `docs/open-source-model.md`.

- Client: `openai.AsyncOpenAI(base_url=settings.openai_compat_base_url, api_key="unused")`. Model name from `OPENAI_COMPAT_MODEL` (for example `Qwen/Qwen3-Coder-30B-A3B-Instruct`).
- vLLM service: `vllm/vllm-openai` with `--model … --enable-auto-tool-choice --tool-call-parser hermes --max-model-len 131072 --guided-decoding-backend xgrammar` (check the vLLM docs for the parser that matches the Qwen release you use). GPU reservation in compose as in README §12.
- `parse()`: `response_format={"type": "json_schema", "json_schema": {"name": output.__name__, "schema": output.model_json_schema(), "strict": True}}` (vLLM guided decoding). Validate with Pydantic; on failure retry once with the validation error appended; then raise `ProviderError`.
- `run_tools()`: `ManualLoopRunner` in OpenAI format: `tools=[{"type": "function", "function": {"name", "description", "parameters": schema}}]`, `tool_choice="auto"`; on `message.tool_calls`, execute (same `_guarded` wrapper, same hooks, same parallel-safe rule) and append `{"role": "tool", "tool_call_id", "content"}` messages; loop until no tool calls or `max_iterations`.
- Anthropic-defined tools have no schema on this path, so `gateway/tool_schemas.py` declares explicit JSON schemas for `bash` (`command` or `restart`) and `str_replace_based_edit_tool` (`command`, `path`, `file_text`, `old_str`, `new_str`, `insert_line`, `insert_text`, `view_range`) that the same tool implementations accept.
- No thinking, effort, caching, context editing, or compaction on this path. Context is managed by the harness: when the transcript exceeds 70 % of `--max-model-len`, older tool results are replaced by `[cleared]` (client-side equivalent of context editing).
- Prompts: the same `.md` files, plus a provider-specific preamble for open models — shorter instructions, an explicit "call exactly one tool per turn unless reads are independent", and a reminder to call `submit_result`. Keep it in `agents/prompts/_open_model_preamble.md`.
- Pricing: API cost is zero; record GPU-seconds per call in `llm_calls.latency_ms` and an estimated `$/GPU-hour` constant so cost comparisons stay honest.
- `RunCreate.provider: Literal["anthropic", "openai_compat"] = "anthropic"` selects the provider for every role in that run. Hybrid mode (open model as the executor for `kind="fix"` tasks or when over 90 % budget) is a flag off by default.
- Claude never goes through this provider; the Anthropic path uses the official SDK only.

**Tests:** a fake OpenAI-compatible server (`respx`) scripts a tool call and a final answer; `parse()` retry on invalid JSON; bash/editor schemas validate the same inputs the Anthropic path receives; the M1 fixture e2e with `provider=openai_compat` against a local vLLM or Ollama (marker `e2e`).

---

### Step 6.4 — Evaluation harness

**Files:** `evals/tasks/*.yaml`, `evals/run.py`, `evals/swebench.py`, `evals/judge.py`, `evals/report.py`, `evals/results/`, `docs/evals.md`.

**Private suite.** 20–30 tasks across your own repositories and the fixtures, tagged `feature`, `bugfix`, `refactor`:

```yaml
id: auth-jwt-expiry
repo: https://github.com/<you>/autoswe-fixture-python
base: auth-baseline
goal: "Reject expired JWTs on /auth/me and add a test."
verify:
  command: "uv run pytest -q tests/test_auth.py"
  expect_exit: 0
tags: [bugfix, security]
```

`evals/run.py --suite private [--provider …] [--ablate …] [--concurrency 3]` creates runs through the real API, waits, checks out the PR branch, runs `verify.command`, and appends a row per task to `evals/results/<timestamp>.jsonl`: `resolved`, `tasks`, `debug_attempts`, `review_rounds`, `wall_clock_s`, `tokens`, `cost_usd`, `cache_hit_rate`, `pr_url`, `escalation_reason`.

**Ablation flags** map to run options: `--ablate no-debugger` (max_debug_attempts=0), `no-repomap` (v1 tree), `coder-sonnet` (route override), `effort-high` (coder effort), `provider=openai_compat`.

**SWE-bench Lite.** `evals/swebench.py --limit N`: load `princeton-nlp/SWE-bench_Lite` with `datasets`; for each instance, the goal is the problem statement, the repo is the instance's repo URL (fork it once per project, or point at the upstream read-only and let the run push to your fork with the cross-fork option from 6.6), `base_branch` is the instance's `base_commit` (extend `RunCreate` to accept a commit SHA). The run's `diff` artifact becomes `model_patch` in `predictions.jsonl` (`instance_id`, `model_name_or_path`, `model_patch`). Score with the official harness (`python -m swebench.harness.run_evaluation --predictions_path … --run_id …`), which uses its own environment images, so the target repo's test setup does not have to be solved by your sandbox images. Run unattended with a `$3` budget per instance; record cost per instance and per resolved instance.

**LLM-as-judge for PR descriptions** (`evals/judge.py`): a rubric (accuracy against the diff, completeness of testing section, honesty of known issues, rollback concreteness; 1–5 each) scored by Opus 5 through the Message Batches API (`client.messages.batches.create`, results keyed by `custom_id`, never by position) at half price.

**Report** (`evals/report.py`): markdown table per suite with means and medians, and a comparison table for ablations. Paste into README §14; fill X (resolved %) and Y ($ per task) in §15.

---

### Step 6.5 — CLI polish

**Files:** `cli/main.py`, `cli/render.py`, `cli/config.py`, `docs/cli.md`.

- Commands: `run` (`--repo --goal --base --budget --unattended --provider --follow`), `watch`, `status`, `artifacts`, `answer`, `approve`, `reject`, `cancel`, `eval` (wraps `evals/run.py`), `mcp` (stdio server).
- `--json` on every read command; `--follow` on `run` streams like `watch` and exits with the run's status code (0 done, 1 failed, 2 awaiting input).
- Config precedence: flags → env (`AUTOSWE_API`, `AUTOSWE_API_KEY`) → `~/.config/autoswe/config.toml`.
- Rendering: phase banners, one line per tool call, coloured test summaries, `awaiting_input` prompts with the command to answer; `rich` is acceptable here (the CLI is not in the worker).
- Shell completion via typer's built-in support.

---

### Step 6.6 — Hardening, cross-fork PRs, docs, release

**Files:** `repo/github.py` (`--upstream`), `docker-compose.yml` (`prod` profile), `docs/security.md`, `docs/architecture.md` (link to README), `CHANGELOG.md`.

- **Cross-fork PRs.** `RunCreate.upstream: str | None` (`owner/repo`). When set, the branch is pushed to `repo_url` (your fork) and the PR is opened on `upstream` with `head="<fork_owner>:agent/<id>"`. The token needs pull-request write on the upstream; document the fork workflow this enables (never push to upstream).
- **Prod profile.** gVisor on, `KEEP_FAILED_SANDBOX_TTL` short, egress proxy required, API behind TLS (reverse proxy), `API_KEYS` rotated by adding the new key, deploying, then removing the old one.
- **Security doc.** README §9 table with a pointer from each row to the test that proves it (sandbox tests, policy tests, gitleaks gate, approvals, budget tests).
- **Docs index.** `docs/README.md` linking local-dev, observability, mcp, open-source-model, evals, cli, security, numbers.
- **Release.** `CHANGELOG.md` entries per phase, `v1.0.0` tag, a three-minute demo recording script: start from Claude Code, ask it to use autoswe on the fixture, watch the run, open the PR, show the review and security sections, show Langfuse, show the numbers table.

---

## 3.5 What step 6.1 actually built

`api/service.py` came first, before any MCP code existed. The HTTP routes held their logic
inline, so "no logic in the MCP layer" was not achievable without somewhere for it to live;
writing the MCP tools against the routes' internals would have produced a second
implementation of the approve path, whose `_pending` guard is what stops an approval being
replayed against a call the human never saw. The 26 existing control-plane tests were the
behaviour-preservation net and passed unchanged through the extraction.

Then `mcp_bridge/server.py` (13 tools, one resource template), `cli/mcp.py` with the
`autoswe-mcp` console script, and `api/routes/mcp_mount.py` for streamable HTTP at `/mcp`.
`docs/mcp.md` is the reference.

**Departures from this plan, and why** — the full list is `docs/mcp.md` §6:

- `FastMCP` is `MCPServer` in the `mcp` SDK's 2.0 release. Same object, new name.
- `create_run` takes no `provider`. The worker builds its provider from process settings,
  so a per-run choice would be written to `runs.provider` and then ignored — a promise the
  system does not keep. Step 6.3 is where the worker learns to honour it.
- stdio has no `AUTOSWE_API_KEY` check. The process reads `DATABASE_URL` and `REDIS_URL`
  from its own environment; a key supplied by that same environment guards nothing.
- Exact routes rather than `app.mount`, so `http://host/mcp` reaches the server instead of
  a 307 to `/mcp/`.

**Two things found by running it that reading would not have found:**

1. The MCP SDK turns DNS-rebinding protection on by default with a localhost-only
   allow-list. Any deployment behind a real hostname would have answered every MCP request
   with a bare `421 Invalid Host header`. `MCP_ALLOWED_HOSTS` makes it opt-in, defaulting
   off because this transport has no ambient authority for a rebound name to borrow.
2. The session manager's task group in the app lifespan is incompatible with a yielding
   async pytest fixture — anyio refuses to leave a cancel scope from a task that did not
   enter it. It broke *teardown only*, so all 26 existing tests passed and all 26 reported
   errors. `tests/integration/conftest.py::api_app` is the fix.

---

## 3.6 What step 6.2 actually built

`mcp_bridge/config.py` (the file and its rejections), `mcp_bridge/client.py` (sessions,
wrapping, reconnection), `tools/registry.register` (dynamic registration), and the mount
in `orchestrator/worker.py`. `mcp_servers.yaml.example` is the shipped configuration, and
a unit test loads it so the documentation cannot drift out of being valid.

**A contradiction in this plan.** §3 step 6.2's own YAML gives the GitHub server
`roles: [planner, analyzer, pr_writer]` alongside `mutating: [add_issue_comment]`. All
three are read-only roles, so that configuration is refused by the rule the same section
states two paragraphs later — "mounting a mutating MCP tool into `analyzer` fails at
startup". The example here gives it to `coder` and `debugger`, the roles that may act.
`tests/unit/test_mcp_config.py` pins the rejection.

**Two smaller departures.** `${VAR}` expands in `env` values only, not in `command`: the
plan put a database DSN in argv, and an argument is visible in `ps` to every user on the
box. And only `stdio` is mounted — an unsupported `transport` is rejected by name rather
than half-working.

**Found by running it:** a connection whose session had died stayed registered with its
`ready` future already resolved, so every later call found it, found no client, and failed
identically. A worker would have needed restarting because a child process crashed once.
`_Connection.dead` and the replacement in `_connect` are the fix, and the test that found
it asserts the *next* call gets a new child rather than the corpse.

---

## 3.7 What step 6.3 actually built

`gateway/providers.py` (which providers exist, which this build can make, and how),
`RunCreate.provider` with validation at the API, `Deps.using()` and the read in `run_job`
so the worker builds from the row, `run_provider`/`fail_run` for a row that names
something unavailable, the `run_provider` argument on the MCP `create_run` tool, the
`vllm` compose service behind the `gpu` profile, `agents/prompts/_open_model_preamble.md`,
and `docs/open-source-model.md`.

**Deliberately not built: the Anthropic provider.** The project runs on whatever is cheap
or free — a standing constraint since Phase 5, which rewrote its own compaction criterion
rather than take the dependency. `anthropic` is therefore a name `gateway/providers.py`
recognises and refuses, with a message naming what does work: a 422 at the API beats a
`NotImplementedError` in a worker thirty seconds after the caller was told the run had
started.

**Not built: `gateway/manual_loop.py`.** The plan wants the tool loop shared between an
Anthropic provider and this one. With one provider there is nothing to share, and an
abstraction with a single implementation is a guess about the second one.

**Found by mutation testing, not by running.** Four of the first eleven mutations survived
— every one of them a test that passed because the deployment default and the requested
value were the same string, so a route that ignored the request entirely still stored the
right answer. Each is now set up so the two differ. A fifth was worse: the runner reported
`CAUGHT` for a test that no longer existed under that name, because `pytest` exits
non-zero for "no such test" as readily as for a failure. The runner now requires a green
baseline before it mutates.

---

## 3.8 What step 6.4 actually built

`evals/suite.py` (tasks and the rejections), `evals/run.py` (the driver and the `Plane`
protocol it is typed against), `evals/report.py` (pure rendering), `evals/judge.py`,
`evals/swebench.py`, three shipped tasks, `docs/evals.md`, `autoswe eval`, `autoswe mcp`,
and `make eval` / `make eval-report`. `/runs/{id}/detail` gained `cache_read_tokens`,
`cache_write_tokens` and `cache_hit_rate`, which is what lets the harness stay outside the
database.

**Three shipped tasks, not twenty.** See the criterion above. The number is a choice and
it is defended rather than hidden.

**The judge does not use the Batches API.** No Anthropic provider, so the rubric goes
through whatever `LLM_PROVIDER` is configured. `docs/evals.md` §6 also says the thing that
matters more than the mechanism: a judge on the same model as the worker is not
independent, and its scores should be read as such.

**The harness talks to the API and nothing else.** Four endpoints, no database, no
orchestrator import. An eval that reached inside would be measuring a path no user takes,
and the first thing it would stop noticing is a broken API. `evals/run.Plane` is that
dependency written down.

**Seventeen mutations, seventeen caught**, each against a verified green baseline. The
ones worth naming are the accounting: a denominator that counted unverifiable tasks, a
`resolved: null` folded into `true`, a cost-per-resolved that divided by zero successes
and reported the total, and a mean where a median belongs. Every one of those produces a
number that looks reasonable and is wrong, which is the only kind of bug that matters in a
file whose output goes into a README.

---

## 4. Testing plan

| Tier | Coverage | Command |
|---|---|---|
| unit | OpenAI-compat provider with a fake server, tool schemas, MCP tool wrapping, report rendering | `make test` |
| integration | FastMCP server in-process, stub MCP client server with approval, cross-fork PR (mocked GitHub) | `make test-int` |
| e2e | Claude Code over MCP (manual, scripted in `docs/mcp.md`), open-model M1 fixture, private suite, SWE-bench Lite subset | `uv run autoswe eval --suite private`, `uv run python evals/swebench.py --limit 50` |

---

## 5. Demo script

```bash
# 1. MCP from Claude Code
claude mcp add autoswe -e AUTOSWE_API_KEY=$AUTOSWE_API_KEY -- uv run autoswe-mcp
claude
> Use autoswe to add a slugify() helper with tests to https://github.com/<you>/autoswe-fixture-python and tell me the PR link when it is done.
# Claude Code calls create_run → wait_for_run → replies with the PR URL

# 2. Open model, offline
docker compose --profile gpu up -d vllm
uv run autoswe run --provider openai_compat --repo … --goal "Implement subtract(a, b) and slugify(text) so tests pass."
psql "$DATABASE_URL" -c "select distinct provider, model from llm_calls where step_id in (select id from steps where run_id='<id>')"
# → openai_compat | Qwen/Qwen3-Coder-30B-A3B-Instruct

# 3. Numbers
uv run autoswe eval --suite private --concurrency 3
uv run python evals/report.py evals/results/latest.jsonl
uv run python evals/swebench.py --limit 50 && uv run python evals/report.py --swebench
```

---

## 6. Pitfalls

- **MCP stdio servers inherit the environment.** Pass only the variables each server needs (`env:` in the YAML); never the worker's whole environment.
- **Tool name collisions.** Prefix mounted tools; a server exposing `read_file` would otherwise shadow yours.
- **Open models and tool discipline.** Expect more malformed tool calls and forgotten `submit_result` calls. The harness must treat a malformed call as `is_error` with the schema in the message, and end a step that hits `max_iterations` cleanly. Do not tune Anthropic prompts to fix open-model behaviour; use the preamble.
- **`strict` JSON schema on vLLM.** Some schema features (`format`, `pattern`, long `enum`s) are unsupported by guided decoding backends; the contracts were kept flat in Phase 0 for this reason. If a schema is rejected, simplify the contract rather than special-casing the provider.
- **SWE-bench environments.** Instances need specific Python versions and dependencies; do not try to make your sandbox images solve that. Produce patches, score with the official harness.
- **Eval cost.** Fifty SWE-bench Lite instances at a few dollars each is real money; run five first, check the pipeline, then scale. Batch the judge.
- **Numbers in the README.** Report the exact suite, model, effort, date, and cost per task next to every percentage. A number without its conditions is not a result.

---

## 7. Release checklist (`v1.0.0`)

- [ ] All six phase exit checklists ticked and linked from the release notes.
- [ ] README §14 has the private-suite and SWE-bench Lite tables with conditions; §15 has X and Y filled.
- [ ] `docs/security.md` maps every threat in README §9 to a test.
- [ ] A clean clone plus `docs/local-dev.md` reaches a green PR on the fixture in under 30 minutes of setup.
- [ ] The demo recording exists.
- [ ] Tag `v1.0.0`.
