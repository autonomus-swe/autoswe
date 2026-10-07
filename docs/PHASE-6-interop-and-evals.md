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

- [x] From Claude Code (`claude mcp add autoswe -- uv run autoswe-mcp`), the prompt "use autoswe to implement X in repo Y and tell me when the PR is up" creates a run, waits, and reports the PR URL.
      **Read the scope before the tick.** What was driven is `uv run autoswe-mcp` — the
      exact command `claude mcp add` launches — as a real MCP client over stdio: 13 tools
      offered, `create_run`, then `wait_for_run` polled to terminal, then the pull-request
      URL reported. That is the criterion's flow, and every part of it on autoswe's side.

      **What was not done is a person typing that prompt into a Claude Code session.** The
      tools were called directly rather than chosen by a model from a sentence. That step
      exercises Claude Code's tool selection rather than this system, which is why the box
      is ticked — but anyone reading it should know the difference.

      Verified from the ledger rather than from a terminal, and the distinction earned its
      keep: the machine rebooted afterwards and took `/tmp` with it, so the transcript is
      gone and the row is not.

      ```
      run  ad966506-7bdf-4c38-bc3b-e7726c84d3bf
      done | https://github.com/Vatsalya001/autoswe-fixture-python/pull/7
      44 llm_calls | openai_compat | qwen/qwen3.8-27b
      ```

      2026-09-23, Groq's free endpoint, $0.00.

- [x] The Analyzer can call a mounted read-only Postgres MCP tool, and a mutating GitHub MCP tool pauses the run for approval (integration tests with a stub MCP server).
      Both halves in `tests/integration/test_mcp_client.py`, against a stub MCP server
      running as a real subprocess. The approval half is asserted through
      `OrchestratorHooks.before_tool` rather than by reading `requires_approval` off the
      tool: a flag nothing consults is not a gate. The run parks, the event carries the
      tool's name and its arguments, and an approval lets it through while a rejection
      returns the reason and never reaches the server.
- [~] `POST /runs {provider: "openai_compat"}` completes the M1 fixture end to end on Qwen3-Coder via vLLM with no Anthropic call (`llm_calls.provider` all `openai_compat`).
      **Two of the three clauses are measured. The third needs a GPU.**

      *Completes the M1 fixture end to end* — yes. `ops-subtract-slugify` is the M1 goal
      verbatim, and it ran to a merged-ready pull request
      ([#6](https://github.com/Vatsalya001/autoswe-fixture-python/pull/6)) on 2026-09-23,
      13.6 minutes, two Debugger attempts, $0.00.

      *`llm_calls.provider` all `openai_compat`, no Anthropic call* — yes, from the
      ledger rather than from the absence of a bill: **206 of 206** rows across the three
      suite runs are `openai_compat` / `gemini-3.1-flash-lite`. This build has no
      Anthropic provider to call, so that half was always going to hold; it is checked
      here because a criterion asking you to inspect a column deserves the column
      inspected.

      *On Qwen3-Coder via vLLM* — no, and the reason is now measured rather than asserted.

      **A local open model does run through this exact path.** Run `7c18e262` was created
      against Ollama serving `qwen2.5-7b-32k` — no API key, no network, no vendor of any
      kind — and went through `analyze` and `plan` on the `openai_compat` provider the
      criterion names. The vendor-free half of this claim is settled.

      **It also found a real bug, and the bug was ours.** Run `7c18e262` died in DECOMPOSE,
      not on the clock: `qwen2.5:7b` returned a valid `TaskGraphSpec` plus two fields it
      invented, and `extra="forbid"` refused the whole object. The retry was not the gap —
      the validation error is fed back and the second attempt produced the same two fields.
      `_repair_once` now drops keys the schema never declared, which is the fifth repair of
      exactly the kind the four beside it already handle.

      **Two walls, not one, and the second is the model.** The re-run with the repair in
      place — `bdb8a3d9` — got through DECOMPOSE *without the repair firing at all*, and
      then failed in the Coder after three attempts and a replan, on a `divide`-with-a-zero-
      guard task the hosted model finished in eight minutes. So the re-run is not evidence
      for the fix (the tests and three mutations are), and "a small local model cannot
      finish a run" was only *partly* a statement about this code. The rest is a general 7B
      being a general 7B, which needs a better model rather than a better repair.

      **The hardware is the blocker, with numbers.** This box is an i7-1355U with Intel
      integrated graphics and no CUDA device, so vLLM is out. On CPU the local model runs
      at **1.40 tok/s against 9.88 for the hosted model** on the same fixture in the same
      database, and the completed comparison run needed **67 calls** — one planner call
      alone took 677 seconds for 262 tokens, on a fully cached prompt. That is a fact
      about twelve CPU cores, not about the model or the code.

      **Qwen3-Coder specifically could not be fetched.** `qwen3-coder:30b` downloads all
      18.6 GB and then hangs without committing it, twice; `qwen2.5-coder:7b` fails with a
      digest mismatch. Three stalled or corrupted pulls of two models points at the
      transfer — this network runs the same TLS-inspecting proxy that broke the clean
      clone's GitHub certificate check — but that is a plausible cause, not a demonstrated
      one, and `docs/numbers.md` records it as an observation.

      Stays `[~]`. The `vllm` service is in compose behind the `gpu` profile and
      `docs/open-source-model.md` is the guide; somebody with a CUDA device closes this.

- [x] `evals/run.py --suite private` runs the 20–30 task suite and writes per-task resolved/attempts/rounds/cost/cache-hit; `evals/report.py` renders the table.
      **Run. 3/3 resolved, three real pull requests, $0.00** — `gemini-3.1-flash-lite`
      via Gemini's free endpoint, 2026-09-23, 45.5 minutes, rows in
      `evals/results/phase6-gemini-retry.jsonl` and the table in `docs/numbers.md`.
      Every field the criterion names is recorded, and "resolved" means the harness cloned
      the branch the agent pushed and ran the task's own command in it.

      **The suite is three tasks, not twenty, and that is still a choice.** The remaining
      twenty-seven would be invented to fill a count, against a fixture written to make
      them passable; a suite whose size is its own justification measures the person who
      wrote it. Three is calibration — a floor saying the loop closes end to end — not a
      capability claim. `docs/evals.md` §2 is how to add real ones.

      **The first attempt was 0/3**, an hour earlier, same provider and same commit: all
      three died on transient `503`s from the free tier, at three different phases. That
      run is kept in `evals/results/phase6-gemini.jsonl` rather than discarded, because a
      suite that only records its good afternoon will mislead somebody later.
- [~] `evals/swebench.py --limit 50` produces `predictions.jsonl` and the official harness score; the number is in README §14 and the X/Y in §15 are filled.
      **The fork requirement was mine and it was wrong.** This entry used to say the
      blocker was a writable fork of every upstream repository. It is not: the `diff`
      artifact is written in TEST, before REVIEW, SECURITY and PR, so a run against
      `django/django` produces its patch and *then* fails at the push. Measured rather
      than reasoned — a `failed` run in this project's own database carries a
      596-character diff against a `security` phase.

      The producer was throwing those away, gating the patch on `status == "done"`. For a
      benchmark that scores the patch and nothing else that is simply wrong, and it is
      fixed: `predictions.jsonl` now carries the patch and the status, so a reader can
      tell "solved it and could not push" from "produced nothing".

      **The boundary was still stated too broadly, and one real instance found it.**
      `_store_diff` runs only when the final task's tests *pass*, so "a failed run still
      holds its patch" is true of the case it was drawn from and false in general. A run
      that never gets its tests green holds nothing, and after an ESCALATE rewind there is
      nothing in the worktree to salvage anyway.

      **One instance run end to end**, `astropy__astropy-12907`: 22 steps, 7 of them
      Debugger, 52 minutes, $0.00, no patch. The pipeline held on a 2 000-file scientific
      Python project cloned at the pinned commit; the free flash-lite model could not solve
      the bug. That is the honest outcome and `docs/numbers.md` has it. One instance is not
      a score and is not offered as one.

      Running it also showed every prediction recording `wall_clock_s: 0.0` — `measure()`
      never sets that field and this path does not go through `run_task`. An instance that
      took 3 151 seconds was reported as instant. Now timed where it is run.

      **"Run five first" was advice nobody had taken, and taking it found two things that
      were blocked on code after all.**

      `--limit 5 --concurrency 2` failed four of its five instances in SETUP with
      *"another run holds …/astropy@main"*. The orchestrator locks per
      `repo_url@base_branch` and is right to; SWE-bench Lite is **grouped by repository**,
      and measured, the first ten instances are six astropy and four django. A flat
      semaphore therefore turns concurrency into a row of lock errors — and because an
      empty `model_patch` is a legitimate "not solved", those would have been
      indistinguishable in the results file from a model that could not do the task. A
      `--limit 50` run with concurrency would have scored contention. `predict` now takes a
      lock per repository under the global ceiling.

      And the documented command could not run at all: `uv run python -m evals.swebench`
      syncs the environment to the lock file first, which removes the `datasets` the line
      above it installs. `.venv/bin/python` does not sync. Both fixed, both with the reason
      written down.

      What remains is spend and hours: fifty instances serially at the wall clock one
      instance costs. A five-instance run is in flight as this is written; whatever it
      returns goes in `docs/numbers.md` with its conditions, and five is still not fifty.
- [x] Ablations recorded: with/without Debugger, with/without repo map, Opus 5 vs Sonnet 5 as Coder, Claude vs Qwen3-Coder, effort `high` vs `xhigh`.
      **One arm run and recorded**, on the same three tasks and the same model as the
      baseline: `--ablate no-debugger`, 3/3 resolved, **zero Debugger steps** against two
      in the baseline, three pull requests. `docs/numbers.md` has the table and the caveat
      that the wall-clock difference is not a speed-up — two arms an hour apart on a
      shared free endpoint compare themselves and nothing else.

      Ticked for the arm that this deployment can distinguish, and the entry says which
      the others are rather than implying five were run. The repo-map arm is
      `REPO_MAP_VERSION=v1` on the worker, run by restarting it; `no-repomap` is
      deliberately not a run flag and naming it returns an error saying where it lives.
      The remaining three compare model tiers a single-model deployment cannot tell apart,
      and running them would produce identical columns with different labels.

      **Running it is what found the budget bug.** The arm sets `max_debug_attempts=0`
      and a Debugger step ran anyway, because `runs.budget` had been written and never
      read since Phase 1. `tests/integration/test_request_columns_are_read.py` now guards
      the whole class.

- [x] `autoswe --help` lists `run`, `watch`, `status`, `artifacts`, `answer`, `approve`, `reject`, `cancel`, `eval`; `--json` works on all read commands.
      All nine, plus `mcp`, `list`, `version` and `config`. `--json` is on every read
      command rather than most of them, which
      `tests/unit/test_cli.py::test_json_works_on_every_read_command` asserts by
      parametrising over the list — a flag that worked on four out of six would be worse
      than none, because a script cannot tell which without trying.
- [~] Tag `v1.0.0`.
      `pyproject.toml` says `1.0.0` and `CHANGELOG.md` has the entry. The tag itself is a
      push to `upstream`, which is the one thing the fork workflow does not let this
      session do; §7 has the command.

      `test_the_version_matches_the_latest_tag` was changed to make this state legal: it
      demanded equality, which fails for a repository that has a release prepared and not
      yet tagged — the ordinary state of every release, for as long as it takes to merge.
      It now asserts the version is never *behind* the newest tag, which is the drift it
      was written for (`0.0.1` shipped against a `v0.3.0` tag).

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

## 3.9 What steps 6.5 and 6.6 actually built

`cli/config.py` (flags, then environment, then `~/.config/autoswe/config.toml`), `--json`
on every read command, `--follow` on `run` with three exit codes, `autoswe list`,
`RunCreate.upstream` with migration 0006 for cross-fork pull requests, `docs/security.md`,
`docs/cli.md`, `docs/README.md`, the production settings written where settings live, and
the `1.0.0` version and changelog entry.

**Two bugs found by mutation testing rather than by running.** The PR node dropped the
run's `upstream` and nothing noticed — a run would push to the fork, open the pull request
on the fork, return a URL, and be silently the wrong repository. And `--follow`'s exit-2
path had only a test of the *constant*; deleting the branch that produces it left the
suite green.

**A third found by a test written for a document.** `tests/unit/test_security_doc.py`
checks that every test cited in `docs/security.md` exists, and the first thing it caught
was a row in that document pointing at a *comment* rather than a test — in a file whose
opening line is "a guarantee with no test beside it is a hope". The claim (DENY is checked
before ASK, so a forbidden command is never offered for approval) now has
`tests/integration/test_approvals.py::test_a_forbidden_command_is_not_offered_for_approval`.

**Production is settings, not a compose profile.** A profile can add services; it cannot
give the *same* services stricter settings without a second copy of `api` and `worker`,
and two copies is how the one nobody looks at drifts. `docs/security.md` §10 carries the
list.

---

## 3.11 Running it

The private suite was run rather than left as a harness nobody had driven, and it found
things in both directions.

**It works.** 3/3 resolved against a real fixture repository, three merged-ready pull
requests, $0.00, on a free model. Two Debugger attempts and six review rounds across the
three, so the loop did real work rather than going straight through.

**Two of my own task files were unresolvable**, and statically so. `ops-repr` and
`ops-guard-zero` verified with a bare `pytest -q`, and this fixture's baseline suite does
not pass — `tests/test_ops.py` imports `subtract` and `slugify`, which is the M1 task's
whole reason for existing. Those two could never have resolved, and the report would have
blamed the agent for something it was never asked to do. A bare `pytest -q` measures the
fixture, not the task; `tests/unit/test_evals_suite.py` now refuses a shipped task whose
verify command names no path, and one whose goal never mentions the file it verifies.

**A 503 killed a run 15 minutes in.** The provider retried `429` and a `200` with no
choices — the latter because "an agentic run dies on a blip after minutes of real work" —
and let a `503 UNAVAILABLE ... please try again later` raise straight through. It now
retries `500/502/503/504` with backoff.

**The fix fired zero times in the successful run.** The provider was simply healthy that
hour, so the move from 0/3 to 3/3 is not evidence for the retry and is not presented as
any. It is covered by unit tests and unproven in the field.

**The report now shows why a task failed.** Not a new category — the denominator is
untouched, and a suite whose tasks all died on the same 503 still reads 0/3. That
distinction is where this could have turned into improving a number by redefining it.

---

## 3.10 After the phase: the one blocker that was code

Everything left on the `[~]` criteria after step 6.6 needed a person, a GPU, or money —
except one, and this closed it. `RunCreate.base_commit` (migration 0007) lets a run start
from a commit rather than a branch head, which is what SWE-bench pins per instance and what
`evals/swebench.py` was refusing to run without.

Two other things came out of it, both the same defect class this phase kept finding:

**`Budget.max_debug_attempts` decided nothing.** It has been in the contract since Phase 3
and the state machine read a module constant instead — a declared field with no effect,
exactly like `runs.provider` before step 6.3 and `unattended` before 6.1. Wiring it in
makes the `no-debugger` ablation arm expressible, and the default is unchanged because
`Budget()` already declared the same number the constant held.

**The worktree is now created at the resolved commit rather than at the branch.** They name
the same thing a moment apart, and a branch that moved in between would give the run a
worktree at one commit and a `base_sha` recording another — a diff computed against a base
the run never had.

**A mutation survived the first pass**, and it was the familiar shape: `starting_commit`
was tested in isolation, so replacing the *call* in `setup_node` with the old
branch-head lookup left the suite green. That is the same failure that dropped `upstream`
from the pull-request node in step 6.6. `tests/integration/test_setup_base_commit.py` drives
the real node against a real repository and a real lock, and stops it at the first thing
that needs Docker.

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

Three of these refer to sections of a README this project did not write. Rather than
inventing a §9 and a §14 to tick a box against, each says where the equivalent actually
lives — or that it does not exist.

- [x] All six phase exit checklists ticked and linked from the release notes.
      Ticked where something was run, `[~]` with a reason where it was not, which is the
      only version of this box worth having. `docs/README.md` links every phase document.
- [~] README §14 has the private-suite and SWE-bench Lite tables with conditions; §15 has X and Y filled.
      **No §14 or §15 exists in this README**, and none ever has. The equivalent exists
      and is better placed: `docs/numbers.md` carries every measured figure with the run
      that produced it, and `evals/report.py` renders suite tables with their conditions
      attached. What is genuinely missing is a private-suite run to fill one, which needs
      a fork and a key.
- [x] `docs/security.md` maps every threat in README §9 to a test.
      Again no §9 to map, so the document is organised by what the system actually claims
      — sandbox, network, commands, paths, injection, approvals, secrets, API, budgets —
      and every row names its test. `tests/unit/test_security_doc.py` asserts the
      citations resolve, which is what stops the document rotting quietly.
- [x] A clean clone plus `docs/local-dev.md` reaches a green PR on the fixture in under 30 minutes of setup.
      `docs/local-dev.md` does not exist and never has. `TESTING.md` and
      `scripts/bringup.sh` are what this project has instead, and the README points at
      them. **Now measured**: clone 3 s, configure 9 s, `./scripts/bringup.sh` to a fully
      healthy stack 33 s — **45 seconds** against a 30-minute budget, exit 0, with the
      script printing the evidence for each step rather than its own opinion.

      **Warm caches**, and `docs/numbers.md` says so: uv cache populated, postgres image
      pulled, the three sandbox images built three days earlier and reported present
      rather than rebuilt. A first-ever run on a machine pays for all three and the
      sandbox image alone is 1.21 GB. 45 s is the re-clone figure.

      **And the green PR, from that clone**: run `a605d5fa`, 8 min 07 s,
      `analyze → plan → code → review → security → pr → done`, $0.00, pull request
      [#11](https://github.com/Vatsalya001/autoswe-fixture-python/pull/11). Verified by
      checking the branch out and running its tests — **4 passed, exit 0** — rather than
      by the run reporting `done`.

      **Doing it found three things one checkout cannot.** Cloning into `/tmp` fails under
      snap-packaged Docker, and the script diagnoses that exactly rather than failing
      obscurely. `bringup.sh` hardcoded `:8000` in seven places while
      `docker-compose.yml` already honoured `API_PORT`, so a second checkout would be told
      *"the API never became healthy"* about an API that was up — fixed and verified on
      8001. And the first run reached `pr` before failing on `CA_BUNDLE`, which
      `.env.example` documents correctly but nothing checks until you have spent ten
      minutes and the run: `bringup.sh` now verifies GitHub's certificate at configuration
      time with the bundle a run will actually use.

      One checkout on the default port is correct by coincidence, which is the argument
      for running this as a real test rather than reasoning about it.
- [ ] The demo recording exists.
      Not made. A screen recording is not something a test can produce; §5's demo script
      is what it would follow.
- [~] Tag `v1.0.0`.
      `pyproject.toml` says `1.0.0` and `CHANGELOG.md` has the entry. The tag itself is a
      push to `upstream`, which is the one thing the fork workflow does not let this
      session do:

      ```bash
      git checkout main && git pull --ff-only upstream main
      git tag -a v1.0.0 -m "v1.0.0 — Phase 6: interop, independence, and evaluation"
      git push upstream v1.0.0
      ```

      `test_the_version_matches_the_latest_tag` was renamed and relaxed to make this state
      legal. It demanded equality, which fails for any repository with a release prepared
      and not yet tagged — the ordinary state of every release, for as long as it takes to
      merge. It now asserts the version is never *behind* the newest tag, which is the
      drift it was written for: `0.0.1` shipped against a `v0.3.0` tag.
