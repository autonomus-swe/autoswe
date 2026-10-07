# Build Plan — Phases

The [README](README.md) is the *what and why*. These files are the *how, in order*. Each phase ends in something you can run and demo; the next phase does not start until the previous phase's exit checklist is fully ticked.

| Phase | README milestone | Outcome you can demo | Document | Suggested duration |
|---|---|---|---|---|
| 0 | — | Repository, tooling, settings, database schema, contracts, CI. `make check` is green on an empty system. | [PHASE-0-foundations.md](PHASE-0-foundations.md) | 2–3 days |
| 1 | M1 | `POST /runs` → sandbox → Coder edits → tests pass → PR opened. | [PHASE-1-single-agent-loop.md](PHASE-1-single-agent-loop.md) | 6–8 days |
| 2 | M2 | Analyzer, Planner, Decomposer with structured outputs; checkpoints; resume after a crash; live SSE stream. | [PHASE-2-plan-and-state.md](PHASE-2-plan-and-state.md) | 5–7 days |
| 3 | M3 | Parsed test reports, Debugger with hypotheses, attempt budgets, no-progress detection, `ESCALATE`, `ask_user`. | [PHASE-3-verification-loop.md](PHASE-3-verification-loop.md) | 5–7 days |
| 4 | M4 | Two-pass Reviewer, scanner-backed Security agent, fix rounds, PR Writer, gitleaks gate, draft PRs. | [PHASE-4-review-security-pr.md](PHASE-4-review-security-pr.md) | 5–7 days |
| 5 | M5 | tree-sitter repo map, pgvector retrieval, prompt caching, context editing + compaction, task budgets, cost dashboard, per-stack sandboxes. | [PHASE-5-scale-and-cost.md](PHASE-5-scale-and-cost.md) | 6–8 days |
| 6 | M6 | MCP server + client, vLLM/Qwen3-Coder provider, SWE-bench Lite harness, CLI polish, v1.0 release. | [PHASE-6-interop-and-evals.md](PHASE-6-interop-and-evals.md) | 6–8 days |

Total: roughly six to eight weeks of focused solo work. Phases 3 and 4 are where the resume claims come from; do not rush them.

---

## How to use these documents

Every phase file has the same shape:

1. **Where this phase sits** — prerequisites, deliverables, what is deliberately deferred.
2. **Exit criteria** — a checklist. The phase is done when every box is ticked, not before.
3. **Architecture slice** — the part of the README diagram this phase builds.
4. **Work breakdown** — numbered steps (`1.1`, `1.2`, …). Each step names the files it creates, gives interface or code skeletons, lists the tests to write, and gives the command that proves it works.
5. **Testing plan** — what runs in unit, integration, and end-to-end tiers.
6. **Demo script** — the exact commands and the expected output for the milestone demo.
7. **Pitfalls** — things that will bite you in this phase specifically.

Work through steps in order. Each step is a branch and a PR (see Git workflow below). Do not skip the tests listed in a step; later phases assume them.

---

## Cross-phase conventions

These hold from Phase 0 onward. Every phase file assumes them without restating them.

### Repository layout

The layout is README §11 with three small deviations, all for practical reasons:

| README | Actual | Why |
|---|---|---|
| `mcp/` | `mcp_bridge/` | `mcp` is the import name of the MCP Python SDK; a top-level package named `mcp` would shadow it. |
| (not listed) | `core/settings.py`, `core/errors.py` | Settings and shared exception types need a home that is not `api/` or `orchestrator/`. |
| `tests/` | `tests/unit/`, `tests/integration/`, `tests/e2e/` | Tiered so CI can run unit tests on every push and integration tests only when Docker is available. |

Packages are top-level inside the repository root (`api.main:app`, `orchestrator.worker.WorkerSettings`), exactly as the README's compose file uses them. The pyproject declares them explicitly so `uv sync` installs the project as an editable package.

### Toolchain

- Python 3.12. `uv` for environments and lockfile. `ruff` for lint and format. `mypy` in strict mode for `contracts/`, `orchestrator/`, `tools/`, `gateway/`; normal mode elsewhere.
- `pytest` with `asyncio_mode = "auto"`. Markers: `unit` (no I/O), `integration` (needs Postgres, Redis, or Docker via testcontainers), `e2e` (needs a real LLM key and GitHub token; never in CI by default).
- One `Makefile` with `up`, `down`, `migrate`, `lint`, `type`, `test`, `test-int`, `check` (lint + type + unit), `sandbox-image`.

### Naming and identifiers

- Runs: UUID v4. Work branch: `agent/<run-id>`. Sandbox container: `run-<run-id>`. Worktree: `<WORKTREES_DIR>/<run-id>`.
- Tasks: short slugs from the decomposer (`t1-skeleton`, `t2-models`). Steps: UUID. Events: Redis stream IDs.
- Phases: the `Phase` enum from README §4.2, in `orchestrator/state.py`. Never compare phases as strings elsewhere.
- Agent roles: `analyzer`, `planner`, `decomposer`, `coder`, `tester`, `debugger`, `review_pre`, `review`, `security`, `pr_writer`. These are the ten keys of `ROUTES` (`gateway/routing.py:47-57`) and the names on `steps.agent`.

  This line said `decompose` and listed an eleventh role, `triage`, until 2026-10-07.
  `triage` does not exist anywhere in the code. `decompose` is the more instructive of the
  two, because it had a consequence: `DOWNGRADE` is keyed by `ROUTES` names, and
  `gateway/routing.py:68-70` records what the typo would have cost — "that entry would never
  have matched and the decomposer would have stayed on opus while the code looked like it
  downgraded." The code was written against the real key and carries a comment saying the
  document is wrong. Nobody then fixed the document, for three weeks.

### The two-model rule for contracts

Every LLM-facing schema is a pure data model: `extra="forbid"`, no methods that mutate, no runtime state (no `status`, no `attempts`). Runtime state wraps it. Example: the Decomposer returns `TaskGraphSpec` (a list of `TaskSpec`); the orchestrator turns it into `TaskGraph` (adds `status`, `attempts`, `next_ready()`). This keeps the JSON schema sent to the API small and strict, and keeps `RunState` free to evolve without changing prompts.

### Definition of Done (every phase)

- [ ] All steps' tests exist and pass: `make check` and `make test-int`.
- [ ] The phase's demo script runs end to end on a clean checkout following only the phase doc.
- [ ] No secrets in the repo (`gitleaks detect` clean), no PII in logs, required env vars validated at startup.
- [ ] `CHANGELOG.md` has an entry for the phase; the commands in `README.md` still work.
- [ ] The exit checklist **in the phase file** is ticked, and the PR that closes the phase says in prose which boxes moved and why.

The last bullet used to read "ticked in the PR description that closes the phase", and on
2026-10-07 that was checked against every pull request this project has: 112 bodies, of
which **exactly one contains a checkbox at all** (#23, quoting an unticked line as the thing
being fixed) and **none contains a `- [x]`**. There is no `.github/PULL_REQUEST_TEMPLATE`,
so nothing ever asked for one.

What the project has actually done since PR #2 is tick the boxes in the phase file and
narrate the move in the PR. Six phases closed that way. So the bullet was not a standard
Phase 6 failed; it was a sentence describing a practice nobody had, and the honest repair is
to write down the practice rather than to retrofit six PR descriptions. The narration half
is the part worth keeping — a tick with no sentence explaining it is how `[~]` boxes turn
into `[x]` boxes quietly.

Two of the bullets above also referred to "the README's §12". `README.md` has never had
numbered sections; every "README §N" citation in these documents resolves to
[`ARCHITECTURE.md`](ARCHITECTURE.md), which carries §1–§15 and has since the first
substantive commit. The citations were written against a document that got renamed before
it was ever published under that name.

### Git workflow

- Work on a personal fork. `origin` is the fork, `upstream` is the main repo. Never push to `upstream`.
- One branch per step: `phase-1/step-3-tool-layer`. One PR per step; squash is fine for your own project, but keep the step number in the title.
- Commit messages: conventional commits (`feat(sandbox): docker exec with timeout and output cap`).
- Tag the end of each phase: `v0.1.0` (Phase 1), `v0.2.0` (Phase 2), … `v1.0.0` (Phase 6).

### Environment variables

The set grows by phase. **`.env.example` is the list** — it is the file the code is written
against, and where this table and that file disagree, that file is right. The `Read by`
column exists because "validated at startup" was not true of every row and the difference
matters: a `Settings` field is validated, and `Settings` is declared `extra="ignore"`
(`core/settings.py:26`), so a name that is *not* a field is read by nothing and fails
silently.

| Variable | Introduced | Required | Read by | Purpose |
|---|---|---|---|---|
| `DATABASE_URL` | 0 | yes | `Settings` | `postgresql+asyncpg://…` |
| `REDIS_URL` | 0 | yes | `Settings` | `redis://…` |
| `API_KEYS` | 0 | yes | `Settings` | comma-separated API keys accepted by the control plane |
| `LLM_PROVIDER` | 1 | default | `Settings` | `openai_compat` (the default) or `anthropic` |
| `LLM_BASE_URL`, `LLM_MODEL` | 1 | default | `Settings` | any OpenAI-compatible endpoint; this is the vLLM knob too |
| `LLM_API_KEY` | 1 | worker, unless `LLM_PROVIDER=anthropic` | `Settings` | `require_worker()` picks this or `ANTHROPIC_API_KEY` by provider |
| `ANTHROPIC_API_KEY` | 1 | worker, only when `LLM_PROVIDER=anthropic` | `Settings` | optional otherwise; the default provider is not Anthropic |
| `GITHUB_TOKEN` | 1 | worker | `Settings` | push + PR |
| `WORKTREES_DIR`, `REPOS_DIR` | 1 | default | `Settings` | host paths for worktrees and bare clone cache |
| `SANDBOX_IMAGE` | 1 | default | `Settings` | `agent-sandbox:python-3.12` |
| `SANDBOX_RUNTIME` | 5 | optional | `Settings` | `runsc` for gVisor |
| `EGRESS_PROXY_URL` | 5 | optional | `Settings` | allow-listed proxy for dependency installs. Setting it *is* the on switch — `egress_enforced` is derived from it (`core/settings.py:137`), there is no separate flag |
| `KEEP_FAILED_SANDBOX`, `SANDBOX_TTL_S` | 5 | default | `Settings` | a boolean and a number, not one combined setting |
| `EMBEDDING_PROVIDER`, `VOYAGE_API_KEY`, `OLLAMA_URL` | 5 | optional | `os.environ` | `repo/embeddings.py:264-271` reads these directly, so a typo is silent — not a `Settings` field |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 5 | optional | the OTel SDK | tracing |

Four rows of this table named variables that do not exist, until 2026-10-07:
`EMBEDDING_MODEL` (the real name is `EMBEDDING_PROVIDER`), `OPENAI_COMPAT_BASE_URL` and
`OPENAI_COMPAT_MODEL` (`LLM_BASE_URL` and `LLM_MODEL`, with `LLM_PROVIDER=openai_compat`
selecting the path), and `LANGFUSE_*`, whose SDK path was never built — `docker-compose.yml`
records that, and the row outlived it. `ANTHROPIC_API_KEY` was also marked flatly required
when the default provider has never been Anthropic.

None of this broke a run, which is the point worth keeping: every one of these was a wrong
*name in a document*, so the only thing it could break was somebody following the document.

### Security invariants (from Phase 1 onward)

1. No secret ever enters the sandbox container. Test it: `env` inside the container must not show `ANTHROPIC`, `GITHUB`, `DATABASE`, or `REDIS` variables.
2. Every file path from the model is resolved and checked with `is_relative_to(worktree_root)` before any file operation.
3. Every bash command passes the deny-list in `tools/policy.py` before it is sent to the sandbox, and the sandbox has no network when agents run.
4. `git push` is never a tool. The orchestrator pushes, once, in the `PR` node, only to `agent/<run-id>`.
5. Repository content shown to a model is wrapped in an explicit untrusted-content fence.

---

## Phase dependency graph

```
Phase 0 ──► Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 4 ──► Phase 5 ──► Phase 6
foundations  M1 loop     M2 plan     M3 verify    M4 review    M5 scale    M6 interop
                                                    │            │
                                                    └── resume claims come from here ──┘
```

Phases 5 and 6 have internal steps that can be reordered (for example, MCP server before the vLLM provider). Phases 0–4 are strictly sequential.

---

## Progress tracker

Copy this into the project's main issue and tick as you go.

- [x] Phase 0 — foundations (`v0.0.1`, tagged 2026-09-07)
- [x] Phase 1 — single-agent loop (`v0.1.0`, 2026-09-11)
- [x] Phase 2 — plan and state (`v0.2.0`, 2026-09-15)
- [x] Phase 3 — verification loop (`v0.3.0`, 2026-09-16)
- [x] Phase 4 — review, security, PR (`v0.4.0`, 2026-09-18)
- [x] Phase 5 — scale and cost (`v0.5.0`, 2026-09-22)
- [x] Phase 6 — interop and evals (`v1.0.0`, 2026-10-07)

Phase 6 ships with two of its eight exit criteria at `[~]`, and they are `[~]` for reasons
no amount of work on this repository would change: Qwen3-Coder via vLLM needs a CUDA device
(this machine has neither `nvidia-smi` nor `/dev/nvidia*`), and SWE-bench at `--limit 50` is
roughly forty hours run serially — five instances were run instead, scored 0 of 5, and the
row says so. The demo recording is a screencast, and `docs/ARCHITECTURE.md` §15 keeps its
`X`/`Y` placeholders deliberately: 0 of 5 from a single repository is not a score, and
writing one in would be worse than leaving it blank. See
[PHASE-6-interop-and-evals.md](PHASE-6-interop-and-evals.md) §1 for the evidence per box.

These two rows went unticked for fifteen days after `v0.5.0` and `v1.0.0` were both tagged,
which is its own small lesson: **a tracker nobody re-derives is a tracker that lies.** It was
found by auditing this file against `git tag`, not by reading it.
