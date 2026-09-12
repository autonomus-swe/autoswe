# How to test autoswe

Two audiences in one file. **Part A** is the fast answer: five minutes, no API key, no
quota, and you end up looking at the console with real data in it. **Part B** is the
thorough pass. **Part C** is the parts that cost quota, and how not to waste it.

Companions: `MANUAL-TESTING.md` walks each capability in depth;
`PHASE-1-CHECKLIST.md` is the Phase 1 exit-criteria runbook; `docs/PHASE-2-*.md` §1
records what is verified and what is not.

> Every command below was executed on this machine before being written down. Where
> something is a known limitation rather than a step, it says so.

**What you need**

| | |
|---|---|
| Docker | for Postgres, Redis, and the agent sandbox |
| Python | 3.12 via `uv` |
| API key | `dev-key-change-me` — the dev default in `.env` |
| LLM quota | **not needed for Part A or B** |

---

## Part A — see it working in five minutes

Three terminals. Keep them open; later parts reuse them.

### 1. Terminal 1 — bring up the stack

```bash
cd ~/Desktop/proj/phase_0
make up && make migrate
```

Expect `Container phase_0-postgres-1 Healthy`, `Container phase_0-redis-1 Healthy`, then
Alembic migration output. Both containers must say **Healthy** — `make up` waits for
them, so if it returns you are fine.

### 2. Terminal 1 — start the API and the console

```bash
uv run uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Leave it running. Check it from terminal 2:

```bash
curl -s localhost:8000/healthz
# {"status":"ok","checks":{"database":"ok","redis":"ok"}}
```

Both checks must read `ok`. If `database` is not ok, step 1 did not finish.

### 3. Open the console

Visit **<http://127.0.0.1:8000/>** and paste `dev-key-change-me` into the
**X-API-Key** box, top right. The key is held in `localStorage`, so you do this once
per browser.

The left rail fills with runs, newest first. It will be empty on a fresh database —
step 4 fixes that.

### 4. Terminal 2 — put a run on the screen

A real agent run costs requests from a small free allowance, and the states worth
looking at (a live stream, a task half-finished, a run parked on a question) are the
awkward ones to reach on demand. So there is a seeder that writes the same rows and
publishes the same events the orchestrator does, with no model involved:

```bash
cd ~/Desktop/proj/phase_0
uv run python scripts/seed_demo_run.py
```

It prints the run id and a URL, then about 40 seconds of phase changes. **Click the top
run in the rail while it is still going** — that is the interesting view. You should see:

- **phase chips** filling in left to right: `setup analyze plan decompose code test pr done`
- **Live events** appending as they happen, the header saying `streaming`
- **Tasks** appearing at `decompose` and turning `done` one at a time
- **Tool calls** and **Model turns** tables growing
- the totals row counting up — 23 tool calls, 31 model turns, ~84k tokens in

When it finishes the header switches to `finished` and a **pull request** link appears
next to the branch name.

> The seeder is a fixture, not a simulator. It proves nothing about the orchestrator —
> the test suite does that. It exists so the interface can be looked at.

### 5. Answer a parked run from the UI

This is the one interaction that is otherwise hard to reach:

```bash
uv run python scripts/seed_demo_run.py --ask
```

It stops at `plan` and waits. In the console the status pill reads `awaiting_input` and a
panel appears — *"The planner needs an answer"* — with the two questions. Type anything
and press **Send**. The panel becomes *"Answer sent — waiting for the run to pick it
up"*, the seeder prints `answer received`, and the run carries on to `done`.

The same thing from the CLI, if you would rather:

```bash
export AUTOSWE_API_KEY=dev-key-change-me
uv run autoswe answer <run-id> "Return every modal value; whitespace-separated."
# accepted
```

### 6. Cancel one, and see one fail

```bash
uv run python scripts/seed_demo_run.py --speed 2     # then hit Cancel in the console
uv run python scripts/seed_demo_run.py --fail        # ends failed, with t3 red
```

**Cancel** appears in the header of any run that is not finished. The run stops at the
next task boundary and the pill turns `cancelled`. `--fail` leaves the last task `failed`
and shows the error banner — worth seeing once, so you recognise it later.

### 7. Watch a run from the terminal

```bash
uv run python scripts/seed_demo_run.py &          # or use a run already going
uv run autoswe watch <run-id>
```

```
phase_changed    analyze
agent_started    analyzer
tool_call        search_code
test_report      passed — 3 tests, 0 failing
pr_opened        https://github.com/.../pull/42
run_finished     done · $0.0000
```

`watch` exits non-zero if the run did not pass, so it composes in a shell. Pointed at a
finished run it replays the history and exits; pointed at a live one it follows, and
reconnects from its cursor if the stream drops.

---

## Part B — the thorough pass, still no quota

### 8. The automated suite

```bash
make sandbox-image     # once; the Docker-backed tests skip without it
uv run pytest -m "unit or integration"
# 274 passed, 4 deselected
```

`make sandbox-image` matters more than it looks. Without
`agent-sandbox:python-3.12` every test behind `requires_docker` **skips** while the run
still reports success — that is how twelve tests went unnoticed in CI. To check you are
really running them:

```bash
uv run pytest -m integration 2>&1 | tail -1
# 54 passed, 224 deselected     <- 54, and no "skipped"
```

If the count is much lower and you see `skipped`, the image is missing.

Tiers, if you want them separately:

```bash
uv run pytest -m unit           # fast, no Docker
uv run pytest -m integration    # real Postgres, Redis, Docker sandbox
make lint && make type          # ruff + mypy strict
```

### 9. The two tests worth understanding

Most of the suite is ordinary. These two carry the phase:

```bash
uv run pytest tests/integration/test_resume.py -v
```

`test_sigkill_after_plan_resumes_without_replanning` starts a worker in a child process,
waits until `PLAN` and `DECOMPOSE` are durable, then sends it a **real `SIGKILL`**. An
in-process exception would prove nothing — `runner.run` catches those, marks the run
failed and tears the sandbox down, which is exactly what a crash does not do. After the
kill the run is still marked `running` and the repo lock is still held; resume then
re-attaches and finishes with exactly one analyzer, planner and decomposer step. Takes
about two minutes.

```bash
uv run pytest tests/integration/test_full_run.py -v -k cancel_stops
```

Cancel has to land within one tool call, not one node — a coder loop runs up to 60
iterations. This one cancels a run that is stuck inside its tool loop and checks the
container is gone afterwards.

### 10. The endpoints by hand

```bash
export K=dev-key-change-me
curl -s -H "X-API-Key: $K" localhost:8000/runs | head -c 300          # list
curl -s -H "X-API-Key: $K" localhost:8000/runs/<id>/detail | jq keys  # one run in full
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/runs          # 401 without a key
curl -N -H "X-API-Key: $K" localhost:8000/runs/<id>/events            # SSE
```

`/detail` answers with `run`, `tasks`, `steps`, `events`, `tool_calls`, `llm_calls` and
`totals` in one request — the console draws itself from that single call, then follows
the event stream.

Two behaviours worth poking at:

```bash
# the stream accepts the key as a query parameter, because EventSource cannot set headers
curl -s -o /dev/null -w '%{http_code}\n' "localhost:8000/runs/<id>/events?key=$K"   # 200
curl -s -o /dev/null -w '%{http_code}\n' "localhost:8000/runs/<id>/events?key=nope" # 401

# answering a run that is not waiting is a conflict, not a silent no-op
uv run autoswe answer <finished-run-id> "hello"
# error: 409 run is done, not awaiting input
```

The query-parameter key is deliberately limited to the read-only stream: query strings
end up in access logs and browser history, so every other route requires the header.

### 11. Reset the data

The seeded runs are labelled `DEMO — …` so they are easy to find:

```bash
docker compose exec -T postgres psql -U postgres -d autoswe \
  -c "delete from runs where goal like 'DEMO%';"
```

Deleting a run cascades to its events, steps, tool calls and tasks.

---

## Part C — testing against a real model

This is where it costs something, and where the honest limits are.

### 12. What the free tiers actually allow

Measured, not quoted from a pricing page:

| | |
|---|---|
| **OpenRouter free** | 20 requests/**minute** and **50 requests/day**. Roughly three agent runs. |
| **Hugging Face** | **Cannot run this agent at all.** A free account cannot make one tool-calling request: the gate is a pre-flight cost estimate, and a single tool definition trips it even with a tiny prompt on the zero-rate provider, pinned. See `docs/PHASE-2-plan-and-state.md` §1 for the measurements. |
| **GitHub Models** | Retired — `410 github_models_retirement_brownout`. |

The per-minute limit is handled: the gateway reads how long the window has left and
waits it out, verified absorbing twelve of them in one run. The **daily 50 is the wall**,
and it is not something a test can wait for, so the retry clamps and gives up rather
than parking your suite for hours.

**Spend it deliberately.** One e2e test on a fresh day's quota, not the pair:

```bash
# the cheap one: only needs ANALYZE and PLAN
uv run pytest -m e2e tests/e2e/test_m2.py -k pauses -v

# the expensive one: a full multi-task coding loop, will likely exhaust the day
uv run pytest -m e2e tests/e2e/test_m2.py -k decomposes -v
```

Check what is left before you start:

```bash
curl -s -X POST https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $(grep ^LLM_API_KEY .env | cut -d= -f2-)" \
  -H 'content-type: application/json' \
  -d '{"model":"openrouter/free","messages":[{"role":"user","content":"hi"}],"max_tokens":1}' \
  | head -c 200
```

A `429` mentioning `free-models-per-day` means wait for 00:00 UTC. `$10` of credit
raises the cap to 1000/day.

### 13. A real run, end to end

Needs a worker as well as the API, so this is terminal 3:

```bash
uv run arq orchestrator.worker.WorkerSettings
```

Then start a run — from the console's **New run** button, or:

```bash
uv run autoswe run \
  --repo https://github.com/Vatsalya001/autoswe-fixture-python \
  --goal "Implement subtract(a, b) and slugify(text) in fixture/ops.py so that tests/test_ops.py passes. Do not change the tests."
```

Watch it in the console. A real run differs from the seeder in ways worth noticing: the
model turns show a real model name and real latencies, tool calls have real durations,
and `cost` is where you would see spend — except it reads `$0.00`, which is the next
item.

### 14. Known gaps

Not defects to discover; they are recorded so you do not waste time on them.

- **`cost_usd` always reads `$0.00`.** `gateway/pricing.py` only knows Anthropic model
  prices, and nothing in use is an Anthropic model. Accurate for a free model, wrong in
  principle, and it has to be fixed before Phase 3 budgets mean anything.
- **The multi-task e2e test has never passed against a real model.** Written, lint and
  type clean, but no free provider has had the quota. `docs/PHASE-2-plan-and-state.md`
  §1 records it.
- **The Planner does not pause when it should.** Given `"Add authentication."` with
  `unattended=False` it planned and started coding instead of asking. The phase notes
  warn about a planner that asks too *many* questions; the opposite is also a failure.
- **Docker here is snap-packaged.** It refuses `no-new-privileges` and cannot bind-mount
  `/tmp`, both already handled in code — do not re-fix them. Worktrees live under
  `$HOME/.autoswe`.
- **A local Redis holds 6379**, so `.env` uses `REDIS_PORT=6380`.
