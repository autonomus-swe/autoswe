# Phase 1 verification checklist

Work down this list and tick the boxes as you go. Steps 0–4 need only Docker. Steps 5–6
use the live model. Step 7 is the last unticked Phase 1 exit criterion; steps 8–9 close
the phase.

`MANUAL-TESTING.md` is the companion: same ground, organised by capability rather than as
a sequence, with more detail per area.

> **Status, 2026-09-10.** Steps 0–7b verified. Step 7c passed once against real GitHub —
> [fixture PR #1](https://github.com/Vatsalya001/autoswe-fixture-python/pull/1), agent
> changed `fixture/ops.py` only, `tests/` untouched. A second consecutive pass is pending
> free-tier quota. Five harness bugs were found and fixed along the way: empty provider
> responses, a missing terminal tool call, two kinds of gateway-rejected output, and
> malformed tool arguments poisoning the message history.

**Current setup**

| | |
|---|---|
| Provider | OpenRouter, `openrouter/free` |
| Free quota | 50 requests/day — **about 3 agent runs**, resets 05:30 IST |
| GitHub token | set (reuses your `gh` login) |
| Corporate TLS proxy | `CA_BUNDLE` set — required here, see 7a |
| Test suite | 180 tests: 141 unit, 37 integration, 2 end-to-end |

> **Budget warning.** Steps 0–4 and 6 use no quota at all — run them freely. Steps 5 and
> 7c each cost a whole run (10–20 requests). With ~3 runs a day, **run step 5 once at
> most, or skip it**, and spend what is left on step 7c, which is the exit criterion.
> A run resends its whole history each turn, so cost grows with the square of turns.

---

## Local verification (no GitHub account needed)

### [ ] 0. Start the stack — ~30s

```bash
cd ~/Desktop/proj/phase_0
source .venv/bin/activate
make up && make migrate && make sandbox-image
```

Both containers report `Healthy`. Re-runs are near-instant.

### [ ] 1. Lint, types, unit tests — ~20s

```bash
make check
```

Expect: `All checks passed!`, `100 files already formatted`, mypy `no issues found in 99
source files`, `141 passed`.

Proves every pure-logic guarantee: the bash deny-list, path confinement, editor
staleness, the test-report parser, pricing, phase transitions, prompt rendering.

### [ ] 2. Integration tests — ~60s

```bash
make test-int
```

Expect `37 passed`. Real Postgres and Redis, a real Docker sandbox, real worktrees.
Includes the migration-versus-models drift check and the API's auth and rate limiting.

### [ ] 3. The whole loop, model scripted — ~20s

```bash
uv run pytest tests/integration/test_full_run.py -v
```

Expect `3 passed`. The single most informative test: clones a real repository, starts the
sandbox, edits through the tools, runs the suite, commits, pushes, opens a pull request,
then asserts the database holds every action in order. Also covers a failing suite ending
the run without a pull request, and the repo lock blocking a second run.

No model is called, so this costs nothing and never flakes on a provider.

### [ ] 4. Inspect the sandbox by hand — ~15s

```bash
uv run python - <<'EOF'
import asyncio, os, uuid
from pathlib import Path
from sandbox.docker import DockerSandbox

ws = Path.home() / ".autoswe" / "tmp" / "manual"; ws.mkdir(parents=True, exist_ok=True)

async def main():
    box = DockerSandbox(uuid.uuid4(), ws, image="agent-sandbox:python-3.12",
                        network="agent-install", user=f"{os.getuid()}:{os.getgid()}")
    await box.start()
    print("whoami           ", (await box.exec("id -u")).stdout.strip())
    print("capabilities     ", (await box.exec("grep CapEff /proc/self/status")).stdout.strip())
    print("write /etc       ", (await box.exec("touch /etc/x")).exit_code, "(non-zero = read-only)")
    print("write /workspace ", (await box.exec("touch /workspace/ok")).exit_code)
    env = (await box.exec("env")).stdout
    leaked = [k for k in ("ANTHROPIC","GITHUB","DATABASE_URL","LLM_API_KEY") if k in env]
    print("host secrets     ", leaked or "none (correct)")
    print("network before   ", await box.has_network())
    await box.disconnect_network()
    print("network after    ", await box.has_network(),
          "| dns:", (await box.exec("getent hosts pypi.org")).exit_code)
    print("timeout kills    ", (await box.exec("sleep 30", timeout_s=2)).timed_out)
    await box.stop()

asyncio.run(main())
EOF
```

Expect: capabilities all zero, `/etc` refused and `/workspace` writable, host secrets
`none`, network `True` then `False`, timeout `True`.

A `sandbox_no_new_privileges_unsupported` warning is expected on this machine.
Snap-packaged Docker refuses that one flag, so the sandbox drops it and says so. Every
other boundary is unchanged.

---

## Real model (Groq, ~10 requests per run)

### [ ] 5. End to end with a live model — ~40s

Step 3 already proved this whole loop with the model scripted, so on a tight free quota
this step is optional: its only extra coverage is that a *live* model can drive the tools.
Run it **once**, then move on. Skip it entirely if you have fewer than three runs left.

```bash
uv run pytest -m e2e tests/e2e/test_m1_local.py -v -s
```

Expect a line like `3 tests, 13 tool calls, 15 model turns, $0.0000`. Turn counts vary a
lot run to run (10–20 with this model); that is normal. The checklist asks for two
consecutive passes.

A `gateway_rejected_tool_call` or `reminding_agent_to_submit` warning in the log is not a
failure: it is the loop correcting a model stumble and carrying on. `openai/gpt-oss-120b`
trips one on nearly every run and still finishes — verified over six consecutive runs on
2026-09-10.

A real model reads the repository, writes `subtract` and `slugify`, gets the suite green,
commits and pushes.

### [ ] 6. Drive the control plane — ~5 min

Only the API is needed here. **The worker will refuse to start until you set
`GITHUB_TOKEN` in step 7a** — that is deliberate, and you start it in step 7c. The checks
below never reach the worker.

Terminal 1:

```bash
cd ~/Desktop/proj/phase_0 && source .venv/bin/activate && make api
```

Terminal 2:

```bash
curl -s localhost:8000/healthz | python3 -m json.tool

# no key -> 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/runs \
  -H 'content-type: application/json' -d '{}'

# non-GitHub URL with a valid key -> 422
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/runs \
  -H 'X-API-Key: dev-key-change-me' -H 'content-type: application/json' \
  -d '{"repo_url":"https://gitlab.com/a/b","goal":"Implement subtract(a, b)"}'

# five per key per minute, then 429
for i in $(seq 6); do curl -s -o /dev/null -w '%{http_code} ' \
  -H 'X-API-Key: dev-key-change-me' \
  localhost:8000/runs/00000000-0000-0000-0000-000000000000; done; echo
```

Expect `database: ok` and `redis: ok`, then `401`, then `422`, then
`404 404 404 404 429 429`.

Four 404s, not five: the bucket allows five authenticated requests per key per minute, and
the `422` call above already spent one of them. The `401` costs nothing because a request
with no key is rejected before it reaches the limiter. Wait a full minute before the burst
and you get the clean `404 404 404 404 404 429`.

The audit trail is read in step 7, once a real run has written to this database. The tests
you ran in steps 2, 3 and 5 use throwaway containers, so the compose database is still
empty at this point — that is expected, not a fault.

---

## The last exit criterion

### [ ] 7. End to end against real GitHub — ~15 min

The only path never yet exercised: opening a genuine pull request through the GitHub API.
Every other test uses a stub.

**7a.** Give the worker a GitHub token. Simplest route, since `gh` is already logged in
and its token carries `repo` scope:

```bash
gh auth status | grep -B1 'Active account: true'      # confirm the right account
sed -i "s|^GITHUB_TOKEN=.*|GITHUB_TOKEN=$(gh auth token)|" .env
uv run autoswe config | grep github_token             # -> set
```

That token reaches every repo you own. For a scoped alternative, create a fine-grained
token at <https://github.com/settings/tokens?type=beta> with **Contents: read and write**
and **Pull requests: read and write** on just the fixture repo, and paste that instead.

**Behind a corporate TLS-inspecting proxy** — `git push` succeeds but the GitHub API fails
with `CERTIFICATE_VERIFY_FAILED`, because git trusts the proxy's root CA from the system
store and Python's bundled certifi does not. Point both at the system bundle:

```bash
grep -q '^CA_BUNDLE=' .env \
  && sed -i 's|^CA_BUNDLE=.*|CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt|' .env \
  || echo 'CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt' >> .env
uv run autoswe config | grep ca_bundle
```

The worker logs `ca_bundle_applied` at startup when this is in effect. Check whether you
need it with `echo | openssl s_client -connect api.github.com:443 2>/dev/null | grep issuer=`
— an issuer that is not a public CA means you are behind such a proxy.

**7b.** Create the fixture repository:

```bash
cd /tmp && rm -rf fixture-build
cp -r ~/Desktop/proj/phase_0/tests/fixtures/fixture_repo fixture-build && cd fixture-build
git init -q -b main && git add -A && git commit -q -m "chore: fixture project"
gh repo create Vatsalya001/autoswe-fixture-python --private --source=. --push
```

**7c.** Run it, with the API and worker from step 6 still up:

```bash
cd ~/Desktop/proj/phase_0 && source .venv/bin/activate
export AUTOSWE_API_KEY=dev-key-change-me
export AUTOSWE_FIXTURE_REPO=https://github.com/Vatsalya001/autoswe-fixture-python
make test-e2e
```

It prints a pull request URL. Open it: the agent should have implemented both functions
and left `tests/` untouched.

`make test-e2e` runs only the GitHub test. `make test-e2e-all` adds the local one from
step 5, which costs a second run and can trip OpenRouter's 20-requests-per-minute free
model limit when the two run back to back.

Run it a second time if quota allows — the Phase 1 checklist asks for two consecutive
passes, and this is the step that should get them.

**7d.** Now the audit trail has something in it. Every bash command and every edit is a row
in `tool_calls`; every model turn is a row in `llm_calls`, both in the order they happened:

```bash
docker compose exec postgres psql -U postgres -d autoswe \
  -c "select phase, status, cost_usd, pr_url from runs order by created_at desc limit 5" \
  -c "select seq, name, exit_code, duration_ms from tool_calls order by seq limit 20" \
  -c "select model, input_tokens, output_tokens, cost_usd, stop_reason from llm_calls order by seq limit 20"
```

If the worker exits immediately saying `FATAL: missing or invalid settings: GITHUB_TOKEN`,
step 7a did not take effect. That message is deliberate — a worker without its secrets
refuses to start rather than accepting a run and dying halfway through it.

---

## Close the phase

### [ ] 8. Merge the pull request

```bash
gh pr merge 3 --repo autonomus-swe/autoswe --rebase
```

Or open <https://github.com/autonomus-swe/autoswe/pull/3> and click **Rebase and merge**.
Rebase is the only merge method enabled.

### [ ] 9. Tag `v0.1.0`

Tell me once #3 is merged and I will create the release and tick the Phase 1 checklist in
`docs/PHASE-1-single-agent-loop.md` and `docs/PHASES.md`.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ValueError: bad marshal data` on import | A corrupt bytecode cache. `find .venv -name '__pycache__' -type d -exec rm -rf {} +` |
| `no choices` from the model | Transient provider hiccup. The gateway retries three times before giving up. |
| `coder did not submit a result` | The model did the work then stopped talking. The loop now reminds it twice before failing; if you still see this, the model is too weak for the tool set. |
| `tool_use_failed` / `output_parse_failed` (Groq) | Groq judges the model's output server-side and answers 400. The loop corrects the model and continues, up to three times per run. Frequent hits mean a stronger model is warranted. |
| `Rate limit exceeded: ...-per-day` | Daily free quota gone. Wait for the reset, or switch `LLM_*` in `.env` to another provider. |
| Model 404 with "use this slug instead" | The model was withdrawn. List live ones: `curl -s $LLM_BASE_URL/models -H "Authorization: Bearer $LLM_API_KEY"` |
| `repository not found` from `gh` | The active account flipped. `gh auth switch --user Vatsalya001` |
| Docker bind-mount fails on `/tmp` | Snap Docker cannot see `/tmp`. Keep `WORKTREES_DIR` and `REPOS_DIR` under `$HOME`. |
