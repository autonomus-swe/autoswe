# What is built, and how to see it for yourself

Every command here has been run on a clean checkout, and every expected output is what it
actually printed — not what it ought to print. Where reality differs from the design, the
difference is in the text.

Read Part 0 and Part 1 to see it alive in ten minutes. Read Part 6 before you plan
anything: it is the list of what is missing, and it is longer than the list of what works.

### None of this calls a model, except Part 5

**Parts 0 to 4 make no request to any LLM and cost nothing.** That is deliberate, and it is
checkable rather than a promise — every provider in the unit and integration tiers is a
subclass that overrides `_complete`, the single method that would make the HTTP call, so
the whole suite runs against a dead endpoint:

```bash
# nothing listening on port 1, and both provider keys removed from the environment
env -u LLM_API_KEY -u ANTHROPIC_API_KEY LLM_BASE_URL=http://127.0.0.1:1/v1 \
  uv run pytest -m "unit or integration"
# 547 passed, 7 deselected in 172s
```

Everything green with no endpoint to call. The dead `LLM_BASE_URL` is the load-bearing
half: any real request would fail to connect.

The consequence cuts both ways, and it is the honest headline of this document:

| what Parts 0-4 prove | what they cannot prove |
|---|---|
| the machinery — every boundary, parser, gate, budget and transition | that a model uses it well |
| that a failing test becomes frames, a signature and a class | that the Debugger reads them and forms a useful theory |
| that a hypothesis is required before an edit | that the hypothesis is any good |
| that the transition table routes every state (100% of branches) | that a real run converges rather than escalating |

**Part 5 is the only part that calls a model**, and it is the one thing still unproven: no
model available here gets past DECOMPOSE. See Part 6's last section.

One number in Part 4 did come from a live model run and is labelled where it appears — the
cancel timing, because the 14-second tail *is* a model call unwinding and a scripted
provider would not have shown it.

---

## Where the product is

| phase | what it does | state |
|---|---|---|
| **0 — foundations** | settings, typed errors, contracts, logging, schema, sandbox image, CI | **done** |
| **1 — single-agent loop** | tools, sandbox, worktree, Coder, gateway, opens a PR | **done** |
| **2 — plan and state** | Analyzer/Planner/Decomposer, checkpoint/resume, events, API, console, CLI | **done** (1 criterion partial) |
| **3 — verification loop** | parsed failures, Debugger, escalation, approvals, budgets, cancel | **done** (1 criterion unproven) |
| **4 — review, security, PR** | reviewer, `bandit`/`semgrep`/`pip-audit`, fix rounds, a real PR body | **done** (`v0.4.0`, 2026-09-18) |
| **5 — scale and cost** | concurrency, caching, model downgrade, GC, metrics | **done** (`v0.5.0`, 2026-09-22) |
| **6 — interop and evals** | MCP server and client, SWE-bench-style evals, open-model provider | **done** (`v1.0.0`, 2026-10-07; 2 of 8 criteria `[~]`) |

In one sentence: **it can take a goal, plan it, write the code, run the tests, debug a
failure, review and scan the result, and open a pull request** — and the two things it
cannot show you are both measurements rather than features: Qwen3-Coder on a GPU this
machine does not have, and a fifty-instance SWE-bench run nobody has sat through.

> ### This document stops at Phase 3, and that is the part to know before reading on
>
> **Parts 0 to 6 below were written and verified at the end of Phase 3 (2026-09-16).** Every
> command in them was really run and every output is really what printed — that promise
> still holds for what is here. What is *missing* is three phases of it: nothing below walks
> you through the Reviewer, the security scanners, the repo map, pgvector retrieval, the MCP
> server, or the eval harness, because those did not exist when it was written.
>
> Until 2026-10-07 the table above said Phases 4, 5 and 6 were "not started — 41 of 41
> criteria open", and `README.md` sends new readers here first. So the single most likely
> first impression of a finished project was that half of it had not been built. **A
> runbook's status table ages faster than its commands, and it is the part a new reader
> believes hardest.**
>
> For the three phases this document does not cover, the per-phase documents do, each with
> its exit criteria and the evidence per box:
> [Phase 4](docs/PHASE-4-review-security-pr.md) ·
> [Phase 5](docs/PHASE-5-scale-and-cost.md) ·
> [Phase 6](docs/PHASE-6-interop-and-evals.md). Measured figures live in
> [docs/numbers.md](docs/numbers.md), the security claims and their tests in
> [docs/security.md](docs/security.md).
>
> Those three phases have not been given Parts here rather than described from the code,
> because the whole value of this file is that somebody ran every line in it. Writing
> plausible transcripts for features I have not driven by hand would make the other six
> parts worth less.

---

## Part 0 — bring it up

```bash
git clone https://github.com/autonomus-swe/autoswe && cd autoswe
cp .env.example .env         # then fill in the three required values, see below
./scripts/bringup.sh
```

**Already have the repository? Skip the clone.** The line above is for a first read. A
second checkout on one machine needs its own `POSTGRES_PORT` / `REDIS_PORT` and matching
`DATABASE_URL` / `REDIS_URL`, or it collides with the first — and cloning *inside* an
existing checkout also breaks `mypy`, which then sees two copies of every module.

Every command in this document runs **from the repository root** — the directory holding
`pyproject.toml`. One directory up there is no project for `uv` to find, so `uv run pytest`
picks up a bare interpreter and dies on `ModuleNotFoundError: No module named 'structlog'`,
which looks like a missing dependency and is really a missing `cd`.

**Keep the checkout under `$HOME` if your Docker came from snap.** A snap-packaged Docker
client is confined and cannot read paths outside your home directory, so the compose file
is invisible to it and `docker compose` reports `no configuration file provided` about a
file you can see with `ls`. The bring-up script detects this and says so. It is the same
confinement that makes `WORKTREES_DIR` have to live under `$HOME`.

The script is idempotent, reads its ports from `.env` rather than assuming, and prints the
evidence for each step rather than only its own opinion. Re-run it any time.

```
== Preflight — is this machine able to run it at all
  ✓ docker 29.6.1 — daemon reachable
  ✓ docker compose 5.3.1
  ✓ uv 0.11.17
  ✓ git 2.43.0
  ✓ .env exists

== Configuration — which settings are present
  ✓ DATABASE_URL set
  ✓ REDIS_URL set
  ✓ API_KEYS set
  ✓ an LLM key is set — provider=openai_compat, model=qwen2.5:7b
  ✓ GITHUB_TOKEN set — a run can push a branch and open a pull request
    ports from .env: postgres=5432 redis=6380

== Python environment
  ✓ dependencies installed (Python 3.12.12)
  ✓ autoswe 0.3.0

== Infrastructure — Postgres, Redis, the install network
  ✓ postgres running, published on :5432
  ✓ redis running, published on :6380
  ✓ 127.0.0.1:5432 accepts connections
  ✓ 127.0.0.1:6380 accepts connections
  ✓ docker network 'agent-install' exists — the sandbox installs dependencies on it, then is disconnected

== Database schema
  ✓ migrations applied — 0002 (head)

== Sandbox image — where the agent's code actually runs
  ✓ agent-sandbox:python-3.12 present (756MB, built 28 hours ago)

== API and console
  ✓ GET /healthz -> {"status":"ok","checks":{"database":"ok","redis":"ok"}}
  ✓ console at http://127.0.0.1:8000/ -> HTTP 200
  ✓ GET /runs with no key -> HTTP 401 (auth is enforced)

== Worker
  ✓ worker_started — "model": "qwen2.5:7b"
  ✓ listening for jobs on redis :6380
```

Other subcommands:

```bash
./scripts/bringup.sh status       # processes, containers, health, disk used by run state
./scripts/bringup.sh seed         # put a finished run on the console — no model, no quota
./scripts/bringup.sh seed --ask   # ...and one parked on a question, so you can answer it
./scripts/bringup.sh logs         # tail the API and worker
./scripts/bringup.sh down         # stop the API and worker, keep the data
./scripts/bringup.sh reset        # stop everything and delete the volumes
./scripts/bringup.sh infra-only   # Postgres, Redis and migrations, nothing else
```

### The three settings that are actually required

`DATABASE_URL`, `REDIS_URL`, `API_KEYS`. Without them nothing loads, and the failure names
the missing one:

```bash
# `env -u DATABASE_URL` is not enough: pydantic-settings reads .env from disk, so the
# variable comes back and the process starts happily. Passing env_file=None is what
# actually exercises the fail-fast path.
env REDIS_URL=redis://localhost:6379/0 API_KEYS=dev-key-change-me \
  uv run python -c "from core.settings import load_settings; load_settings(None)"; echo "exit=$?"
# FATAL: missing or invalid settings: DATABASE_URL
# exit=2
```

`LLM_API_KEY` (or `ANTHROPIC_API_KEY`) and `GITHUB_TOKEN` are needed **only by the worker**,
which exits at startup naming whichever is missing. The API, the console and both test
suites work without them. The bring-up script says so rather than starting a worker that
will die.

The sandbox image is built once (`make sandbox-image`, a few minutes). Without it the unit
suite still passes and the sandbox integration tests skip.

---

## Part 1 — see it alive (no model, no spend)

The interesting states of a run are awkward to reach on demand, so there is a seeder that
writes the same rows and publishes the same events the orchestrator does.

```bash
./scripts/bringup.sh seed
```

```
  phase -> analyze
  phase -> plan
  phase -> decompose
  phase -> code
  phase -> test
  phase -> code
  phase -> test
  phase -> code
  phase -> test
  phase -> pr
  phase -> done
  done
```

Open **http://127.0.0.1:8000/** and paste the key from `API_KEYS` into the field. You get a
rail of runs on the left and one run in full on the right: phase chips, stat tiles, three
task cards, a live event feed, and every tool call and model turn.

It is a fixture, not a simulator — it proves the interface, not the orchestrator. The tests
in Part 2 prove the orchestrator.

The same run through the API, which is what the console is reading:

```bash
export KEY=$(grep -m1 ^API_KEYS= .env | cut -d= -f2- | cut -d, -f1)
RUN=$(curl -s -H "X-API-Key: $KEY" localhost:8000/runs | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["run_id"])')
curl -s -H "X-API-Key: $KEY" "localhost:8000/runs/$RUN/detail" | python3 -c '
import json,sys; d=json.load(sys.stdin)
print("phase/status :", d["run"]["phase"], "/", d["run"]["status"])
print("counts       :", {k: len(d[k]) for k in ("tasks","steps","events","tool_calls","llm_calls")})
print("tasks        :", [(t["id"], t["status"]) for t in d["tasks"]])'
```

```
phase/status : done / done
counts       : {'tasks': 3, 'steps': 6, 'events': 52, 'tool_calls': 23, 'llm_calls': 31}
tasks        : [('t1', 'done'), ('t2', 'done'), ('t3', 'done')]
```

### The endpoints, by hand

```bash
curl -s localhost:8000/healthz
# {"status":"ok","checks":{"database":"ok","redis":"ok"}}

curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/runs                       # 401
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-API-Key: wrong' localhost:8000/runs # 401
curl -s -o /dev/null -w '%{http_code}\n' -H "X-API-Key: $KEY" localhost:8000/runs  # 200

curl -s -o /dev/null -w '%{http_code}\n' -H "X-API-Key: $KEY" localhost:8000/runs/nope
# 422   — a malformed uuid is rejected at the boundary
curl -s -o /dev/null -w '%{http_code}\n' -H "X-API-Key: $KEY" \
  localhost:8000/runs/00000000-0000-0000-0000-000000000000
# 404
```

The event stream, both ways — `EventSource` cannot set headers, so the console passes the
key as a query parameter:

```bash
curl -s --max-time 5 -H "X-API-Key: $KEY" -H 'Last-Event-ID: 0-0' \
  "localhost:8000/runs/$RUN/events" | head -6
curl -s --max-time 5 "localhost:8000/runs/$RUN/events?key=$KEY" | head -6
```

```
id: 240
event: phase_changed
data: {"phase":"analyze"}

id: 241
event: agent_started
```

From the terminal instead, which exits non-zero when the run did not pass:

```bash
uv run autoswe watch "$RUN"; echo "exit: $?"
```

```
phase_changed    analyze
agent_started    analyzer
agent_finished   error: RunCancelled: cancelled before running read_file
run_finished     cancelled · $0.0000
exit: 1
```

All nine CLI commands: `version`, `config`, `run`, `status`, `watch`, `answer`, `approve`,
`reject`, `cancel` (`uv run autoswe --help`). `autoswe config` prints the whole effective
configuration with every secret masked.

---

## Part 2 — the automated suites (no model, no spend)

```bash
make lint && make type && make test && make test-int
```

```
All checks passed!                      # ruff
144 files already formatted             # ruff format --check
Success: no issues found in 143 source files   # mypy --strict
446 passed, 100 deselected               # unit,       ~3s
 93 passed, 453 deselected               # integration, ~2m30s
```

The integration suite is not a mock where it matters: a real Docker sandbox, a real
Postgres from testcontainers, a real Redis, real git worktrees. **No test skips** on a
machine with Docker and the sandbox image — if you see skips, the image is missing.

The one thing it *does* fake is the model. Every provider in both tiers is a subclass
overriding `_complete`, so the loop, the hooks, the tool dispatch and the structured-output
handling are all real while the turn itself is scripted. That is what makes 547 tests
runnable in three minutes for nothing — and what leaves the agent's judgement untested.

Secrets, across the whole history rather than the working tree:

```bash
gitleaks git --config .gitleaks.toml --redact .
# 103 commits scanned.
# no leaks found
```

Coverage, if you want the number: **92%** over 8,738 statements. The gaps are the outer
shells — CLI bodies, the SSE loop, worker bootstrap — which is why Part 1 exists.

---

## Part 3 — the guarantees you should doubt (no model, no spend)

These are the claims that matter, so each one is a command rather than a paragraph.

### The sandbox really is a sandbox

One script checks eight boundaries at once. It starts a real container, inspects it from
the host, and then asks the container about itself.

```bash
uv run python - <<'PY'
import asyncio, json, os
from pathlib import Path
from sandbox.docker import DockerSandbox

WS = Path.home() / ".autoswe" / "proof-ws"
HOME = str(Path.home())
CHECKS = [
    ("id -u", "uid inside"),
    ("grep CapEff /proc/self/status", "capabilities"),
    ("grep NoNewPrivs /proc/self/status", "no_new_privs"),
    ("touch /etc/proof; echo exit=$?", "write /etc"),
    ("touch /usr/bin/proof; echo exit=$?", "write /usr/bin"),
    ("touch /tmp/proof; echo exit=$?", "write /tmp"),
    ("touch /workspace/proof; echo exit=$?", "write /workspace"),
    ("df -h /tmp | tail -1", "/tmp filesystem"),
    ("cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/cpu.max /sys/fs/cgroup/pids.max", "cgroup mem/cpu/pids"),
    ("env | cut -d= -f1 | sort | paste -sd,", "env names inside"),
    ("env | grep -iE 'anthropic|github|database|redis|api_key|token' || echo '(none)'", "secret-shaped env"),
    (f"ls -d {HOME} 2>&1 | head -1", "host HOME inside"),
]

async def main() -> None:
    WS.mkdir(parents=True, exist_ok=True)
    sb = DockerSandbox("proof", WS, image="agent-sandbox:python-3.12",
                       network="agent-install", user=f"{os.getuid()}:{os.getgid()}")
    await sb.start()
    try:
        a = sb.container.attrs
        keys = ("CapDrop", "ReadonlyRootfs", "Tmpfs", "Memory", "NanoCpus", "PidsLimit", "SecurityOpt")
        print("Config.User        ", a["Config"]["User"])
        print("HostConfig         ", json.dumps({k: a["HostConfig"][k] for k in keys}, sort_keys=True))
        print("Mounts             ", [(m["Type"], m["Source"], m["Destination"], m["RW"]) for m in a["Mounts"]])
        for cmd, label in CHECKS:
            r = await sb.exec(cmd)
            print(f"{label:19}", (r.stdout + r.stderr).strip())
    finally:
        await sb.stop()

asyncio.run(main())
PY
```

```
Config.User         1001:1001
HostConfig          {"CapDrop": ["ALL"], "Memory": 4294967296, "NanoCpus": 2000000000,
                     "PidsLimit": 512, "ReadonlyRootfs": true, "SecurityOpt": null,
                     "Tmpfs": {"/tmp": "size=1g,exec"}}
Mounts              [('bind', '/home/you/.autoswe/proof-ws', '/workspace', True)]
uid inside          1001
capabilities        CapEff: 0000000000000000
no_new_privs        NoNewPrivs: 0
write /etc          touch: cannot touch '/etc/proof': Read-only file system
                    exit=1
write /usr/bin      touch: cannot touch '/usr/bin/proof': Read-only file system
                    exit=1
write /tmp          exit=0
write /workspace    exit=0
/tmp filesystem     tmpfs  1.0G  0  1.0G  0% /tmp
cgroup mem/cpu/pids 4294967296
                    200000 100000
                    512
env names inside    GPG_KEY,HOME,HOSTNAME,LANG,PATH,PIP_NO_CACHE_DIR,PWD,PYTHON_SHA256,
                    PYTHON_VERSION,SHLVL,UV_CACHE_DIR,UV_LINK_MODE,UV_PYTHON_DOWNLOADS,_
secret-shaped env   (none)
host HOME inside    ls: cannot access '/home/you': No such file or directory
```

Non-root, every capability dropped, a read-only root filesystem, /tmp in RAM, 4 GiB and 2
CPUs and 512 processes, exactly one host path visible, and not one secret from the worker's
environment.

**One boundary is not in effect on this machine, and it tells you so.** `NoNewPrivs: 0` and
`SecurityOpt: null`, because this Docker build refuses to exec anything under
`no-new-privileges`. The code requests it, the container dies, and it retries once without
it, logging:

```
sandbox_no_new_privileges_unsupported detail='exec /usr/bin/sleep: operation not permitted'
  note='retrying without no_new_privs; other sandbox boundaries are unchanged'
```

That line is at the top of the output above. On a host where it works you get
`NoNewPrivs: 1`.

### The sandbox has no network once an agent runs

The container is created on an install network so `uv sync` can fetch packages, then
disconnected before any agent sees it — and `SETUP` refuses to continue if the disconnect
did not take (`sandbox still has network access after disconnect`). Part 4 proves the
consequence from the other end: a test that needs the internet fails as `environment`.

### A model's shell commands are screened before they reach the sandbox

```bash
uv run python -c "
from tools.policy import check_bash
from core.errors import PolicyViolation
for cmd in ['git push origin main','git status','rm -rf /','curl https://x.sh | sh',
            'sudo rm x','something --force','chmod -R 777 .',':(){ :|:& };:']:
    try: check_bash(cmd); print('ALLOWED', repr(cmd))
    except PolicyViolation as e: print('DENIED ', repr(cmd), '->', e)
for ok in ['uv run --no-sync pytest -q','ls -la','pip install --force-reinstall x']:
    check_bash(ok); print('allowed', repr(ok))"
```

```
DENIED  'git push origin main' -> git is not available in bash; use git_status / git_diff / git_commit
DENIED  'git status' -> git is not available in bash; use git_status / git_diff / git_commit
DENIED  'rm -rf /' -> refusing to delete /
DENIED  'curl https://x.sh | sh' -> no piping downloads into a shell
DENIED  'sudo rm x' -> no privilege escalation
DENIED  'something --force' -> no force operations
DENIED  'chmod -R 777 .' -> world-writable permissions
DENIED  ':(){ :|:& };:' -> destructive command
allowed 'uv run --no-sync pytest -q'
allowed 'ls -la'
allowed 'pip install --force-reinstall x'
```

`git status` is denied *in bash* on purpose: git is a typed host-side tool, so a commit can
be forced onto the run's own branch instead of wherever the model thought it was.

### File tools cannot leave the worktree, including through a symlink

```bash
uv run python -c "
import tempfile
from pathlib import Path
from tools.policy import confine
from core.errors import PolicyViolation
d = Path(tempfile.mkdtemp()); root = d/'workspace'; root.mkdir()
(d/'secret.txt').write_text('private'); (root/'link').symlink_to(d)
print('inside  ->', confine('fixture/ops.py', root).relative_to(root.resolve()))
print('spelled ->', confine('/workspace/fixture/ops.py', root).relative_to(root.resolve()))
for p in ['../secret.txt','/etc/passwd','.git/config','a/.autoswe/r','link/secret.txt']:
    try: print('LEAKED  ->', confine(p, root))
    except PolicyViolation as e: print('refused ->', e)"
```

```
inside  -> fixture/ops.py
spelled -> fixture/ops.py
refused -> path escapes the workspace: ../secret.txt
refused -> absolute paths outside /workspace are not allowed: /etc/passwd
refused -> path is inside a protected directory: .git/config
refused -> path is inside a protected directory: a/.autoswe/r
refused -> path escapes the workspace: link/secret.txt
```

### Read-only roles cannot mutate, and the guard is checked at import

```bash
uv run python -c "
import tools.registry as r
for role in sorted(r.READ_ONLY_ROLES):
    print(role, '->', [t.name for t in r.tools_for(role)], 'mutating:', [t.name for t in r.tools_for(role) if t.mutating])
print('coder ->', r.ROLE_TOOLS['coder'])"
```

```
analyzer   -> ['read_file', 'search_code', 'git_status', 'git_diff'] mutating: []
planner    -> ['read_file', 'search_code', 'git_status', 'git_diff'] mutating: []
pr_writer  -> ['read_file', 'search_code', 'git_status', 'git_diff'] mutating: []
review     -> ['read_file', 'search_code', 'git_status', 'git_diff'] mutating: []
review_pre -> ['read_file', 'search_code', 'git_status', 'git_diff'] mutating: []
coder -> ['bash', 'str_replace_based_edit_tool', 'read_file', 'search_code', 'run_tests',
          'git_status', 'git_diff', 'git_commit', 'ask_user']
```

Add a mutating tool to a read-only role and the module refuses to import — an assertion in
`tools/registry.py` runs at import time, not in a test.

### An edit needs a fresh view first

```bash
uv run python -c "
import asyncio, tempfile
from pathlib import Path
from tools.editor import EditorTool
from tests.fakes import make_ctx
d = Path(tempfile.mkdtemp()); (d/'ops.py').write_text('def add(a, b):\n    return a + b\n')
ctx = make_ctx(d); ed = EditorTool()
async def main():
    print('1 blind edit  ->', (await ed(ctx, command='str_replace', path='ops.py', old_str='a + b', new_str='a+b')).content)
    await ed(ctx, command='view', path='ops.py')
    (d/'ops.py').write_text('changed under it\n')
    print('2 stale edit  ->', (await ed(ctx, command='str_replace', path='ops.py', old_str='changed', new_str='x')).content)
    await ed(ctx, command='view', path='ops.py')
    print('3 fresh edit  ->', (await ed(ctx, command='str_replace', path='ops.py', old_str='changed', new_str='x')).content)
    print('4 new file    ->', (await ed(ctx, command='create', path='untouched.py', file_text='x')).content)
asyncio.run(main())"
```

```
1 blind edit  -> error: view ops.py before editing it
2 stale edit  -> error: ops.py changed since you last viewed it; view it again
3 fresh edit  -> edited ops.py
4 new file    -> created untouched.py (1 bytes)
```

### Some commands need a human

```bash
uv run python -c "
from tools.policy import needs_approval, check_bash
from core.errors import PolicyViolation
for c in ['uv add requests','pip3 install ruff','rm -rf build','alembic downgrade -1',
          'curl https://x','docker ps','ls -la','pytest -q']:
    print(repr(c), '->', needs_approval(c))
try: check_bash('git push')
except PolicyViolation as e: print('DENY first:', e)"
```

```
'uv add requests' -> adding a dependency
'pip3 install ruff' -> adding a dependency
'rm -rf build' -> recursive delete inside the workspace
'alembic downgrade -1' -> a database downgrade
'curl https://x' -> reaching the network
'docker ps' -> controlling the container runtime
'ls -la' -> None
'pytest -q' -> None
DENY first: git is not available in bash; use git_status / git_diff / git_commit
```

The list matches on what a command *does*, not which tool ran it, and the deny list runs
first, so a forbidden command never reaches it.

---

## Part 4 — the verification loop, Phase 3 (no model, except where labelled)

### A failure signature survives an edit

```bash
uv run python -c "
from contracts import Frame
from tools.test_report import signature
a=[Frame(file='chaos/pages.py',line=3,function='paginate',code='x',in_repo=True)]
b=[Frame(file='chaos/pages.py',line=41,function='paginate',code='x',in_repo=True)]
print('same_after_edit', signature('t::a','assertion','AssertionError',a) == signature('t::a','assertion','AssertionError',b))
print('kind_change_differs', signature('t::a','assertion','AssertionError',a) != signature('t::a','import','AssertionError',a))"
```

```
same_after_edit True
kind_change_differs True
```

A Debugger edits code and lines move. "The same failure" means the same test failing the
same way in the same function — which is what lets the run notice that an attempt changed
nothing.

### A failure's class comes from the message, then the traceback

```bash
uv run python -c "
from tools.test_report import classify, guess_kind
print('message_wins   ', classify('socket.gaierror: Temporary failure in name resolution',
                                  'with urlopen(url, timeout=5) as r:'))
print('traceback_used ', classify('', 'E   Failed: Timeout >5.0s'))
print('bare_timeout_kw', guess_kind('resp = get(url, timeout=5)'))
print('import_before_assertion', guess_kind('ImportError while importing test module\nE assert 1 == 2'))"
```

```
message_wins    environment
traceback_used  timeout
bare_timeout_kw exception
import_before_assertion import
```

A traceback quotes source code, and source code contains words. Reading both at once once
let `urlopen(url, timeout=5)` outvote `socket.gaierror`, so a run with no network was told
it had a timeout — and the Debugger is told `environment` failures are not its to fix while
being told nothing of the sort about `timeout`.

### The baseline never excuses the job

```bash
uv run pytest tests/unit/test_tester.py -q
# 42 passed
```

```
dropped ['tests/test_legacy.py::test_old'] kept ['tests/test_pages.py::test_last'] passed False
all_inherited_is_a_pass True
empty_signature_never_forgiven []
collected_nothing_is_never_a_pass False
no_selector_excuses_nothing []
```

Failures the repository already had are excused. The task's own selector never is. With no
selector, **nothing** is excused — the tests a goal names are failing before it starts;
that is what makes it a goal.

### The Debugger cannot touch anything before it has a theory

```bash
uv run python -c "
import asyncio
from uuid import uuid4
from orchestrator.hooks import OrchestratorHooks
from agents.debugger import HYPOTHESIS_KEY
sub = {}
h = OrchestratorHooks(run_id=uuid4(), step_id=uuid4(), engine=None, bus=None,
                      provider_name='p', model='m', effort=None, role='debugger', submitted=sub)
async def main():
    for t in ['bash','str_replace_based_edit_tool','run_tests','git_commit']:
        print('BEFORE', t, '->', (await h.before_tool(t, {}) or '')[:58])
    for t in ['read_file','search_code']:
        print('READ  ', t, '->', await h.before_tool(t, {}))
    sub[HYPOTHESIS_KEY] = object()
    print('AFTER  bash ->', await h.before_tool('bash', {}))
asyncio.run(main())"
```

```
BEFORE bash -> call submit_hypothesis first — bash changes things, and a di
BEFORE str_replace_based_edit_tool -> call submit_hypothesis first — str_repl
BEFORE run_tests -> call submit_hypothesis first — run_tests changes things,
BEFORE git_commit -> call submit_hypothesis first — git_commit changes things
READ   read_file -> None
READ   search_code -> None
AFTER  bash -> None
```

Reading is always allowed; changing anything is not, until the hypothesis exists. A
diagnosis written after the edit is a description of the edit.

### The same failure twice changes the strategy

```bash
uv run pytest tests/unit/test_transition.py --cov=orchestrator.transition --cov-branch --cov-report=term -q
```

```
same_signature -> debug   strategy alternative
new_signature  -> debug   strategy None
passing        -> pr      strategy None

Name                         Stmts   Miss Branch BrPart  Cover
orchestrator/transition.py      51      0     34      0   100%
51 passed
```

**51 cases, 100% branch coverage, measured.** `transition()` is a pure function of the run
state: it does no I/O and the model never chooses the next phase.

### The five chaos fixtures, in the real sandbox

```bash
uv run pytest tests/integration/test_chaos_in_sandbox.py -q
# 5 passed
```

One fixture repository, a branch per scenario, installed in the real sandbox with the
network cut:

| branch | what is wrong | what the parser reports |
|---|---|---|
| `a-off-by-one` | the last partial page is dropped | two **distinct** `assertion` signatures, with `assert [[1, 2], [3, 4]] == [[1, 2], [3, 4], [5]]` in the message |
| `b-missing-import` | a module uses `datetime` without importing it | `import`, with `chaos/stamp.py:5` and the line read from disk |
| `c-impossible` | a test asserts a list is both three pages and two | unsatisfiable by construction |
| `d-network` | a test fetches example.com | `environment` — **only checkable here**; it passes on a networked host |
| `e-baseline` | the off-by-one *plus* a test already failing | the inherited failure separates from the task's own |

### A cancel lands mid-command

**The one measurement in this part taken from a live model run** — a scripted provider
returns instantly, so it could never show the tail below. With the worker running against
a real model and a run in flight:

```bash
uv run autoswe cancel "$RUN"
```

```
14:56:01  cancel requested
14:56:03  cancel_killing_sandbox   phase=analyze      ← ~2s, mid-phase
14:56:03  sandbox_exec_killed
14:56:17  run_cancelled            "cancelled while a phase was running"
```

The container is removed and the repo lock released. **The 14 seconds are the model call
unwinding** — see Part 6.

---

## Part 5 — a real run, against a real model (**this one spends**)

```bash
export AUTOSWE_API_KEY=$(grep -m1 ^API_KEYS= .env | cut -d= -f2- | cut -d, -f1)
uv run autoswe run --repo https://github.com/you/some-repo \
  --goal "tests/test_x.py is failing. Fix src/x.py so it passes. Do not change the tests."
uv run autoswe watch <run-id>
```

Read this before you try it:

- **The repository must be an `https://github.com/owner/name` URL.** A local path is
  rejected with `422 repo_url must be an https://github.com/... URL`. That is deliberate —
  a local path submitted through the API would bind-mount arbitrary host directories into a
  sandbox — but it means you need a real GitHub repository and a `GITHUB_TOKEN` that can
  push to it.
- **The model must speak the OpenAI-compatible API.** `LLM_PROVIDER=anthropic` is a name this
  build recognises and refuses, with a message naming the providers that do work
  (`gateway/providers.py:18-19` — a 422 at the API beats a `NotImplementedError` in a
  worker). An Anthropic key is of no use on its own; OpenRouter, Groq, Gemini, Cerebras or a
  local Ollama endpoint are. This row said the provider "arrives in Phase 6" — Phase 6 has
  shipped and it deliberately did not arrive: the project runs on what is cheap or free, and
  `docs/open-source-model.md` is candid that the open-model path is the only path here.
- **A local 7B model will not finish.** Measured on this machine: `qwen2.5:7b` via Ollama got
  through SETUP, ANALYZE (with the baseline) and PLAN in 13 minutes, then failed at
  DECOMPOSE — it returned prose instead of calling the forced `submit_TaskGraphSpec`
  function, twice. Phases it reached worked. See `evals/results/README.md`.
- **`pytest -m e2e` will not skip on a machine with any `LLM_API_KEY` set** — including the
  placeholder one a local Ollama setup uses. It will drive real runs, slowly. Unset the key
  first if that is not what you want.

---

## Part 6 — what is not built, and what will surprise you

This is the part to read before planning. Everything here is verified, not assumed.

### Whole phases missing — no longer true, and the command below is why it is worth keeping

**At the end of Phase 3 this section opened: "Phases 4, 5 and 6 are unbuilt: 41 of 41 exit
criteria open."** All three have since shipped — `v0.4.0`, `v0.5.0`, `v1.0.0`. Re-run the
same command the section used as its evidence:

```bash
grep -c '^- \[ \]' docs/PHASE-4-review-security-pr.md docs/PHASE-5-scale-and-cost.md docs/PHASE-6-interop-and-evals.md
# then: 13, 14, 14
# now:   2,  0,  1          (measured 2026-10-07)
```

The three that remain are open for stated reasons, not for want of code: two in Phase 6 need
a CUDA device and about forty hours, and Phase 4's two are `[ ]` with written reasons rather
than oversights. `REVIEW` and `SECURITY` have nodes. The pull request gets a review, a
security scan and a dependency audit.

### Things that will bite you today

**Read the status column before the row.** This table was verified at the end of Phase 3 and
re-checked on 2026-10-07; ten of its fifteen rows describe gaps that Phases 4–6 closed. They
are kept rather than deleted because a limitation and the date it stopped being one are both
worth knowing — and because one row here is the best example in this repository of why
"evidence" and "conclusion" are not the same thing.

**That row is the MCP one.** Its evidence command, `wc -c mcp_bridge/__init__.py`, still
prints `0` today. The package's `__init__.py` is genuinely empty — and `mcp_bridge/server.py`
next to it offers thirteen tools. The command never stopped being true; it just stopped
measuring the claim it was attached to. A check whose output is stable while the thing it
stands for changes underneath is worse than no check, because it reports *confirmed*.

Rows marked **superseded** were re-measured on 2026-10-07 by running the command in the
evidence column. Rows marked **still true** were re-measured the same way and still hold.

| status | what | evidence |
|---|---|---|
| **still true** | **A pause longer than ~50 minutes is killed, not honoured.** The open-question timeout is 24 h; the queue's job timeout is 50 min. | `orchestrator/worker.py:187  job_timeout = 50 * 60` vs `orchestrator/nodes.py:66  AWAITING_INPUT_TIMEOUT_S = 24 * 3600`. Both line numbers moved; the mismatch did not |
| **not re-run** | **A cancel does not interrupt a model call.** It kills the sandbox command in ~2 s, then waits out the provider request — bounded only by `llm_timeout_s` (600 s default). | Part 4's timings. Needs a live run to re-measure, so it is neither confirmed nor withdrawn here |
| **superseded** | **Nothing is reaped.** One worktree per run that did not push, with its `.venv` inside, plus every stopped container. | Phase 5 shipped the collector: `orchestrator/gc.py`, an arq cron over containers, worktrees and bare clones. `docs/test-gaps.md` §"Beyond Phase 6" covers its clone sweep |
| **claim holds, count stale** | **The dollar budget cannot bound an unpriced model** — only the 45-minute wall clock can. The system knows this rather than reporting a reassuring zero: an unknown model logs `unknown_model_pricing` and the budget drops the dollar dimension (`cost_measurable=False`). A *local* endpoint is different — there a zero is a real measurement. | `PRICES` in `gateway/pricing.py` now has eleven entries, not the three this row says. The behaviour for a model that is *not* in them is unchanged |
| **still true** | **The console cannot approve or reject a tool call.** The endpoints and CLI commands exist; the buttons do not. | `grep -c approve api/static/app.js` → `0`, re-run 2026-10-07 |
| **superseded (CLI only)** | **Nothing shows you the `tool_call_id`** that `autoswe approve` needs. | `grep -c tool_call_id cli/main.py` → `4`. The console still shows nothing: `api/static/app.js` → `0` |
| **superseded** | **`unattended` cannot be set by any client.** Every run created through the API is attended, so escalation always parks rather than failing. | `grep -c unattended api/schemas.py` → `2` |
| **superseded** | **Artifacts are written and nothing can read them.** Four kinds go into the `artifacts` table — `baseline_report`, `test_report`, `test_report_raw` (the unfiltered report, kept precisely so a reviewer can ask *why did this pass*) and `step_input`. There is no `GET /runs/{id}/artifacts`, no `autoswe artifacts`, and no console field, so the only way to read any of it is SQL. The diff itself is not stored at all — it lives only in the worktree. | `api/routes/artifacts.py` exists and `cli/main.py:349` is `def artifacts`. The diff is stored too — SWE-bench reads a run's `diff` artifact |
| **superseded** | **`/metrics` is a placeholder** and needs no key. | `observability/metrics.py:151-153` returns `generate_latest()` with `CONTENT_TYPE_LATEST` — real Prometheus output |
| **superseded** | **There is no MCP server or client.** | `grep -c '@server.tool()' mcp_bridge/server.py` → `13`. **Note the original evidence, `wc -c mcp_bridge/__init__.py` → `0`, is still exactly true** — see the paragraph above this table |
| **superseded** | **Only a Python sandbox image exists.** A repository whose tests are `go test` or `npm test` has nothing to run them in. | `ls sandbox/images/` → `go  node  python` |
| **still true** | **The worker refuses to start without `GITHUB_TOKEN`**, even for a run that would never push. | `core/settings.py  require_worker()` — `missing = [name for name in (key_field, "github_token") ...]`, unchanged |
| **superseded** | **The README still describes Phase 2** and quotes an integration count 39 short. | `head -1 README.md` → `# autoswe` |

### The one unproven claim in Phase 3

Eight of nine exit criteria are met with tests. The ninth — the agent diagnosing and
recovering from a real failure end to end, and giving up honestly on the impossible one —
is written (`tests/e2e/test_m3.py`) and has never passed, because no model available here
can get through DECOMPOSE. `evals/results/m3.jsonl` does not exist, for the same reason and
deliberately: a file of numbers nobody measured is worse than no file.

Everything *below* that claim is proven, including in the real sandbox. What is unproven is
the agent's reasoning, which is the interesting half.

---

## Part 7 — put it back

```bash
./scripts/bringup.sh down     # stop the API and worker, keep the data
./scripts/bringup.sh reset    # ...and delete the volumes

# the things nothing reaps yet
docker ps -aq --filter name=run- | xargs -r docker rm
rm -rf ~/.autoswe/worktrees/*        # only when no run is in flight
```
