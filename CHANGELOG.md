# Changelog

## Unreleased — Phase 6 step 6.3: a run chooses its provider

Added
- `gateway/providers.py`: which providers exist (`KNOWN`), which this build can construct
  (`AVAILABLE`), and how to build one. Two lists because a run row outlives a deployment.
- `RunCreate.provider` and `run_provider` on the MCP `create_run` tool, validated against
  `AVAILABLE` — a 422 or a tool error rather than a run that is accepted and dies in a
  worker the caller cannot see.
- `agents/prompts/_open_model_preamble.md`, prepended to every role prompt in the same
  cached block. Five rules about tool protocol and nothing about the work, because the
  plan is explicit: do not tune role prompts to fix open-model behaviour.
  `OPEN_MODEL_PREAMBLE=false` turns it off.
- A `vllm` service behind the `gpu` compose profile, and `docs/open-source-model.md`.

Fixed
- The worker now builds its provider from the run's row. `runs.provider` had been written
  and ignored since Phase 1, which made `llm_calls.provider` — the column the Phase 6
  criterion asks you to inspect — evidence of the process's configuration rather than of
  the run's.
- A run whose row names a provider this build cannot make is failed with that message,
  rather than burning three arq retries and staying `queued`.

Notes
- The Anthropic provider is deliberately absent and `anthropic` is a name that is
  recognised and refused. `gateway/manual_loop.py` is absent too: the plan wants the tool
  loop shared between two providers, and an abstraction with one implementation is a guess
  about the second.

## Unreleased — Phase 6 step 6.2: external MCP servers as agent tools

A GitHub server that can read issues, a Postgres server that can run a read-only query:
tools the agents did not have to be written to know about. `docs/mcp.md` Part B.

Added
- `mcp_bridge/config.py` and `mcp_servers.yaml.example`. Every field is a policy the
  harness enforces — `allow` is the boundary, `roles` decides who gets them, `mutating`
  drives approval, `env` is everything the child process receives. Read from the file and
  never from the server's own annotations: a server that declared its write tool read-only
  would otherwise be disarming its own approval gate.
- `mcp_bridge/client.py`: one session per server per worker process, each in a supervisor
  task that owns its cancel scope, shared across runs, replaced when it dies.
- `tools.registry.register`, so a mounted tool is subject to the harness rather than
  beside it. `orchestrator/hooks.py` reads `requires_approval` out of the registry; a tool
  that never got there is a `None` in that lookup, and a `None` is approved by nobody.
  A rejected registration is rolled back whole.

Fixed
- A connection whose session had died stayed registered with its `ready` future already
  resolved, so every later call found it, found no client, and failed identically — a
  worker needing a restart because a child process crashed once.

Notes
- The Phase 6 plan's own example routes a mutating GitHub tool to `planner`, `analyzer`
  and `pr_writer`, all read-only roles. That configuration is refused by the rule the same
  section states two paragraphs later; `mcp_servers.yaml.example` gives it to `coder` and
  `debugger`. `${VAR}` expands in `env` only, not in `command` — the plan put a database
  DSN in argv, where `ps` shows it to everyone on the box.

## Unreleased — Phase 6 step 6.1: the control plane as an MCP server

An editor can drive a run: start it, wait for it, answer its questions, approve the tool
calls it asks about, and read the diff and the PR URL. Over stdio (`autoswe-mcp`, a child
process) or streamable HTTP at `/mcp`. `docs/mcp.md` is the reference.

Added
- `api/service.py`: the control plane, extracted from the HTTP routes so both transports
  call one implementation. The routes became shape conversion; the 26 control-plane tests
  passed unchanged through the extraction. This came first and on purpose — writing MCP
  tools against the routes' internals would have produced a second copy of the approve
  path, whose replay guard is what stops a human authorising a call they never saw.
- `mcp_bridge/server.py`: 13 tools and the `run://{run_id}/artifacts/{kind}` resource.
- `mcp_bridge/workspace.py`: `search_code` and `read_file` over a run's checkout, routed
  through the agents' own tools so `tools/policy.confine` contains them, rather than a
  second path check to keep in step.
- `cli/mcp.py` and the `autoswe-mcp` console script; `api/routes/mcp_mount.py` for HTTP.
- `RunCreate.unattended`. The column and the orchestrator's handling of it have existed
  since Phase 1 and nothing outside a seed script could set it.
- `MCP_ALLOWED_HOSTS`. The MCP SDK enables DNS-rebinding protection by default with a
  localhost-only allow-list, so the first deployment behind a real hostname would have
  answered every request with a bare `421` naming nothing. Off by default here: the
  transport requires `X-API-Key`, a header no cross-origin page can set, and the app
  installs no CORS middleware.

Fixed
- The HTTP transport is authorised at the ASGI layer, not by a FastAPI dependency. It is a
  separate ASGI application, so a `Depends` beside it would never run and every tool
  including `cancel_run` would have been open while looking guarded.
- `/mcp` and `/mcp/` are both exact routes. A mount would have answered `/mcp` — what goes
  in an editor's configuration — with a 307.

Changed
- `tests/integration/conftest.py::api_app` replaces the per-file `api` fixtures. The app's
  lifespan now holds the MCP session manager's anyio task group, and a yielding async
  fixture is resumed in a different task at teardown, which anyio refuses. It failed on
  teardown only, so every test passed and every test errored.

## 0.5.0 — Phase 5: scale and cost

The system works on repositories it cannot hold in one prompt, and the cost of doing so is
measured rather than estimated. A tree-sitter symbol index, a repo map ranked by centrality
and goal, semantic retrieval in pgvector, prompt caching with hit rates from real runs,
budget-aware routing, tracing and metrics, and per-stack sandboxes.

Seven of ten exit criteria met, one deferred to Phase 6 with the Anthropic provider, one
pending a completed scale run. `docs/PHASE-5-scale-and-cost.md` §6.5 records exactly what
is measured and what is not; `docs/numbers.md` carries every figure with the run it came
from.

Added
- `repo/symbols.py` and `tools/symbols.py`: a tree-sitter symbol index keyed by SHA.
  Django, 7 120 files and 44 292 symbols, parsed in 9.9 s — and measured **on the worker**
  in a real run at 5.4 s, closing a caveat the criterion had carried since it was ticked.
- Repo map v2: PageRank over the import graph mixed 0.6/0.4 with BM25 over the goal, inside
  a token budget. `REPO_MAP_VERSION=v1` selects the ablation arm the phase prescribes,
  which until now could not be run at all.
- `repo/embeddings.py` and `search_code(semantic=True)`, answering the criterion's own
  question on a fixture built so only meaning can answer it.
- `gateway/caching.py`: breakpoints placed most-stable-first. **0.7745 run-level on a local
  model, 0.6178 on a 7 091-file Django clone** — both from the `llm_calls` ledger.
- Budget-aware routing, proven against a server that reports which model answered.
- OpenTelemetry spans, Prometheus metrics, and the `observability` compose profile the demo
  script had always told you to start.
- Node and Go images, an egress proxy that fails closed, container GC, per-profile limits.
- `make scale-run` / `make scale-ablation`, with a preflight that fails in one second on a
  wrong key, model or endpoint rather than after a sandbox and a 3 000-file index.

Fixed
- Nine defects that only running it could find, four of them in one twenty-line block added
  to force a tool call — a second implementation of an existing path omitting the cases the
  first one handles. Notably: a tool call is now returned to the provider with whatever it
  arrived with, because Gemini attaches an opaque `thought_signature` and rejects the next
  turn without it; and the Debugger enforced the tool whose absence it survives instead of
  the one whose absence kills the step.

Corrected
- `CHARS_PER_TOKEN` was the prose figure, so the map criterion was reported as met while
  missed by a third. The Analyzer's "first call" of 43 990 tokens was a whole step summed
  over twelve turns, and a summary read "eight of ten" while the boxes said seven. Each is
  recorded in place rather than quietly edited, and `tests/unit/test_design_notes.py` and
  `test_phase_5_tally.py` now count what the prose claims.

## 0.4.0 — Phase 4: review, security, and a pull request worth reading

Before anything is pushed, the change is reviewed in two passes, scanned by four tools plus
a model that reads their output, fixed in bounded rounds, and described by a writer that is
trusted with prose and nothing else. Secrets do not leave the worker.

The rule running through all of it: **the model judges, the code decides.** Every gate —
`blocking`, `critical`, draft-or-not, every number in the pull request body — is computed
host-side from the model's severities, never read from the model's own answer.

Added
- A two-pass Reviewer. The cheap pass enumerates and is allowed to be wrong; the expensive
  pass opens each file and confirms or rejects with a reason. Rejections are stored in the
  `review` artifact, and a candidate dropped without one is marked as exactly that.
- `repo/diff.py`: the diff a review reads, with untracked files included, generated files
  summarised, and the new-side line numbers kept so a finding can be tied to a line the
  run actually added.
- Four scanners — bandit and semgrep in the sandbox, gitleaks and pip-audit on the worker,
  each placed by what it needs rather than by preference. A scanner that fails becomes an
  `info` finding naming the tool, because silence reads as "nothing wrong".
- A Security agent that checks scanner output against the files, and a `critical` gate
  computed in code: `critical` or `high`, inside the diff, not rejected. An unverified
  finding still gates.
- An eleven-key checklist that reports and never gates by itself. Whether a key applies is
  computed from the diff, because a model that just wrote the code is not the right judge
  of whether its own SQL handling was in scope.
- Fix rounds, bounded at two per gate, with one function owning both budgets. Unresolved
  findings go to `known_issues` and the pull request carries them verbatim.
- A PR Writer with no tools, and `repo/pr_body.py` which renders its prose beside the
  harness's numbers. A run that gave up opens a draft that says so.
- An escalated run with commits opens a draft `[WIP]` pull request carrying the hypotheses
  the Debugger already ruled out.
- `GET /runs/{id}/artifacts` and `/artifacts/{kind}`, plus `autoswe artifacts`. Seven kinds
  were being written and none could be read.
- The `f-injection` chaos branch, with instructions addressed at the agent in three files,
  and a harness test driven by a provider that obeys them on purpose.

Fixed
- **bandit published the values it found.** Its B105/B106/B107 family quotes the matched
  literal in `issue_text`, which was copied into a finding's message and rendered into the
  published pull request body. The case that mattered is the one gitleaks misses: a short
  common password is flagged by B105 and nothing else, so the push was not refused and the
  body shipped the value. Those rules now report the rule and the location, value withheld.
- **The tool ledger could not tell a refused command from a failed one.** Both were
  `exit_code=1` and `approved_by` was never written, so "did anything forbidden execute"
  was unanswerable from the audit trail. A refusal is now `-1`, attributed to `policy` or
  `harness`.
- **The approval list was consulted before the deny list**, so a forbidden command reached
  a human for a decision with no legitimate yes. `policy.py` had claimed the opposite.
- semgrep waited out a connect timeout on a version check it could never complete in a
  networkless sandbox: 2m36s became 1m01s. Narrowing the vendored rules to the security
  set took a 181-file repository from 104.1s to 58.4s.
- The console's phase rail went blank whenever a run reached REVIEW, because the phase was
  in neither of its two lists. A test now reads both lists out of the JavaScript.
- `full_diff` wrote its temporary index into `<worktree>/.git`, which is a *file* in a
  linked worktree, not a directory.

Not fully verified: one exit criterion needs a funded API key — whether a real reviewer
calls a seeded token-expiry bug `blocking` and leaves a style-only diff alone. Both fixture
branches, their premises and the assertion are in place; see
`docs/PHASE-4-review-security-pr.md` §1 and `tests/e2e/test_m4.py`.

## 0.3.0 — Phase 3: the verification loop

A failing test is now something the run reasons about rather than something it reports. It
parses the failure into frames and a stable signature, forms a hypothesis before it edits,
notices when an attempt changed nothing, and stops in a way a human can act on.

Added
- Test reports with stack frames, the source line at each, and a signature that survives
  an edit: test id, failure class, exception type, and the topmost frame in repository
  code — never a line number, because a fix moves them.
- The Debugger, and a gate that refuses every mutating tool until it has called
  `submit_hypothesis`. A diagnosis written after the edit describes the edit.
- No-progress detection: the same report signature twice means the last hypothesis
  changed nothing, and the next prompt says so and rules that class of fix out.
- `ESCALATE`, with a way out of every stuck state — replan the task into smaller ones,
  ask a human, or end the run — and never the same attempt again.
- A baseline test run, so failures the repository already had are not the agent's. Never
  applied to the tests a task was written against, and not applied at all when the task
  named none: the tests a goal is about are failing before it starts.
- Flaky detection: a test that fails in the suite and passes alone is recorded and
  reported rather than silently forgiven, because one re-run cannot tell flaky from
  order-dependent.
- `ask_user`, tool approvals, and an ASK list that matches on what a command does rather
  than which tool ran it. `POST /runs/{id}/approve` and `/reject`, `autoswe approve` and
  `autoswe reject`. Time parked on a human is subtracted from the wall clock.
- Budgets enforced from `llm_calls` at every step boundary and every tenth model turn,
  and inside the tool loop as well: out of budget, an agent may commit and submit but not
  start new work. `budget_warning` once per run at 90 %.
- A cancel that lands during a long command, not just between them.
- Five chaos fixtures — off-by-one, missing import, impossible test, network, pre-existing
  failure — with a test that verifies each still fails the way its README says.

Fixed
- The failure message handed to the Debugger was pytest's advice ("Use -v to get more
  diff") rather than the comparison that explains the failure.
- Every traceback frame was duplicated, because the crash entry reports an absolute path
  where the traceback reports a relative one.
- A collection failure carried no frames at all and was classified `exception`; it is now
  `import`, with the frames parsed out of the printed representation.
- `run_structured` did not record its usage, so the Tester's triage and the Decomposer's
  re-plan were spend the run could not see — which matters now that the ledger is what
  budgets are enforced from.

Known gaps
- `evals/results/m3.jsonl` is not populated: the end-to-end runs need a funded API key.
  `evals/results/README.md` says how to produce it.

## 0.2.0 — Phase 2: plan and state

A run now understands a repository before it edits it. It profiles the repository, plans,
splits the goal into a task graph, checkpoints after every node so a crashed worker
resumes instead of starting over, streams its events live, and stops to ask when the goal
is ambiguous. There is a console to watch it in.

Added
- Read-only tools for the agents that must not write: `read_file` and `search_code`, the
  latter preferring ripgrep and falling back to a Python scan.
- Deterministic repository facts and a ranked repo map. The test command is derived from
  evidence — CI workflow, `pyproject.toml`, `package.json`, `go.mod`, `Makefile` — and
  reports nothing rather than guessing.
- Analyzer, Planner and Decomposer agents, each with a validated contract and one
  correction round.
- `RunState` v2, a checkpoint after every node, and resume that re-attaches the repo
  lock and worktree and replaces the container from the dead attempt.
- Events to a Redis stream and the durable table, `GET /runs/{id}/events` as SSE with
  `Last-Event-ID` replay, `POST /runs/{id}/answer` and `POST /runs/{id}/cancel`.
- A run console at `/`: plain HTML, CSS and JavaScript, no build step. A rail of runs and
  one run in full — phases, tasks, live events, every tool call and model turn, and the
  controls to answer or cancel. Backed by `GET /runs` and `GET /runs/{id}/detail`.
- CLI `watch`, `answer` and `cancel`. `watch` exits non-zero if the run did not pass.
- `scripts/seed_demo_run.py`, which drives a run through every phase with no model, so
  the console can be exercised without spending an allowance.
- `LLM_TIMEOUT_S`. The client timeout was hardcoded at ten minutes and unreachable from
  configuration, which makes a local model unusable — a 7B on CPU needs half an hour for
  one structured-output call.
- `TESTING.md`: five minutes to a console with data in it, then the thorough pass, then
  what the free model tiers actually allow.

Fixed — every one of these was found by running the thing, not by reading it
- Resume destroyed its own work. Refreshing the clone cache pruned the run's own
  unpushed branch, orphaning the worktree on an unborn branch; the run then lost every
  commit it had made and died at push.
- The agent could not install dependencies at all. Every `uv` call in the sandbox
  segfaulted, because `pip install uv` ships a glibc-linked binary and the
  `python:3.12-slim` tag had moved to Debian 13. Both the base image and uv are pinned
  now, and uv comes from Astral's musl build.
- Tool calls were missing from every replay: they were published to Redis only, bypassing
  the table a replay reads from.
- Cancel only landed at node boundaries, so a coder loop could keep editing for minutes
  after someone asked it to stop.
- A submit reminder spent the last turn instead of buying one, so an agent that explored
  to its iteration cap was told to submit with no turn left to do it in.
- Structured output that mirrors the JSON Schema instead of conforming to it is now
  repaired where the fix is unambiguous, rather than discarding a correct answer over its
  wrapper.
- Reads shared the run-creation rate-limit bucket, so the console throttled itself out of
  its own interface within seconds.
- CI ran the integration tier without building the sandbox image, so twelve tests skipped
  while the job reported success; and `gitleaks` had failed on every commit to `main`
  since the repo moved into an organisation, because the action needs a paid licence.

Known gaps
- `cost_usd` records `$0.00` for everything in use: the pricing table only knows
  Anthropic models. Accurate for a free model, wrong in principle, and it must be fixed
  before Phase 3's budgets mean anything.
- The multi-task end-to-end test has not passed in a single run against a real model.
  Both halves are verified separately — the loop completes with a commit per task, and a
  goal decomposes into four or seven tasks — but never together. See
  `docs/PHASE-2-plan-and-state.md` §1 for the attempt log and why.
- The Planner does not pause on a goal as vague as "Add authentication."

## 0.1.0 — Phase 1: single-agent loop

Given a repository URL and a goal in English, an agent clones the repository, edits it
inside a locked-down container, runs the tests, commits, pushes `agent/<run-id>` and
opens a pull request. Every command it runs and every model turn is a row in Postgres.

Added
- Docker sandbox: one container per run, non-root, all capabilities dropped, read-only
  rootfs, memory/CPU/pid caps, only the worktree bind-mounted. Reaches the network only
  while dependencies install, then is disconnected before the agent starts.
- Repository layer: bare clone cache, per-run worktrees, per-process git credentials,
  push restricted to `agent/*`, idempotent pull request creation.
- Tool layer: bash, an editor with view-staleness checks, `run_tests` with a
  pytest-json-report parser, host-side git tools, a deny-list and path confinement.
- LLM gateway over any OpenAI-compatible endpoint, per-role routing, pricing, and
  structured output validated by pydantic.
- Coder agent, the SETUP/CODE/TEST/PR machine with guaranteed teardown, arq worker.
- Control plane: `POST /runs`, `GET /runs/{id}`, `/healthz`, API-key auth with a per-key
  rate limit. CLI `autoswe run` and `autoswe status`.
- Migration 0002: monotonic `seq` on `tool_calls` and `llm_calls` so a run replays in order.
- `CA_BUNDLE` for corporate TLS-inspecting proxies.

Fixed — all found by real runs against live models
- An empty `choices` list on a 200 is retried rather than killing the run.
- An agent that does the work then stops without calling its required tool is reminded.
- Gateway-side rejections (`tool_use_failed`, `output_parse_failed`) are corrected.
- Malformed tool arguments no longer enter the message history, where they poisoned
  every later turn.
- The worker validates its secrets at startup instead of dying mid-run.

## 0.0.1 — Phase 0: foundations

- Project scaffold: uv, ruff, mypy, pytest tiers, Makefile, pre-commit, CI.
- `core/settings.py`: pydantic-settings with fail-fast validation; worker-only secrets.
- `contracts/`: every structured-output schema from README §6 plus runtime wrappers.
- `storage/`: SQLAlchemy 2.0 models for the eight core tables, async Alembic migration `0001`, thin repository functions, Redis helpers (streams, inbox, locks, token bucket, cancel flag).
- `observability/`: structlog JSON logging with secret redaction and run-context binding; OpenTelemetry span helper.
- docker-compose with pgvector Postgres 16 and Redis 7.
