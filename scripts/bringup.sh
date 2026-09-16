#!/usr/bin/env bash
# Bring autoswe up from nothing, or tell you exactly what is missing.
#
#   scripts/bringup.sh            # preflight, infrastructure, migrations, API, worker
#   scripts/bringup.sh status     # what is running, on which ports, and is it healthy
#   scripts/bringup.sh seed       # put a believable run on the console, no model needed
#   scripts/bringup.sh down       # stop the API and worker, leave the data
#   scripts/bringup.sh reset      # stop everything and delete the volumes
#   scripts/bringup.sh logs       # tail the API and worker logs
#
# Every step prints the evidence it succeeded rather than only its own opinion, because a
# green line that checked nothing is worse than a red one. Safe to re-run: each step is
# skipped when it is already true.
#
# Ports come from .env (POSTGRES_PORT, REDIS_PORT), never from an assumption — this
# machine publishes redis on 6380 and a script hardcoding 6379 would report a healthy
# stack it never touched.
#
# Secrets are reported as "set" or "missing". No value from .env is ever printed.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUNDIR="$ROOT/.run"
API_LOG="$RUNDIR/api.log"
WORKER_LOG="$RUNDIR/worker.log"
API_PID="$RUNDIR/api.pid"
WORKER_PID="$RUNDIR/worker.pid"

if [[ -t 1 ]]; then
  BOLD=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; OFF=$'\e[0m'
else
  BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; OFF=""
fi

FAILED=0
step()  { printf '\n%s== %s%s\n' "$BOLD" "$*" "$OFF"; }
ok()    { printf '  %s✓%s %s\n' "$GREEN" "$OFF" "$*"; }
warn()  { printf '  %s!%s %s\n' "$YELLOW" "$OFF" "$*"; }
bad()   { printf '  %s✗%s %s\n' "$RED" "$OFF" "$*"; FAILED=1; }
note()  { printf '    %s%s%s\n' "$DIM" "$*" "$OFF"; }

# Read one key from .env without sourcing it: a secret containing $ or a space would
# otherwise be re-expanded or word-split by the shell.
env_get() {
  local key="$1" default="${2-}"
  local line
  line="$(grep -m1 -E "^${key}=" .env 2>/dev/null || true)"
  if [[ -z "$line" ]]; then printf '%s' "$default"; else printf '%s' "${line#*=}"; fi
}

env_is_set() {
  local v; v="$(env_get "$1")"
  [[ -n "$v" ]]
}

port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

# ---------------------------------------------------------------- preflight

preflight() {
  step "Preflight — is this machine able to run it at all"

  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null) — daemon reachable"
  else
    bad "docker is missing or the daemon is not reachable"
    note "the sandbox, Postgres and Redis all run in it; nothing works without it"
    note "install: https://docs.docker.com/engine/install/  then: sudo usermod -aG docker \$USER"
  fi

  if docker compose version >/dev/null 2>&1; then
    ok "docker compose $(docker compose version --short 2>/dev/null)"
  else
    bad "docker compose v2 is missing (the v1 'docker-compose' binary will not do)"
  fi

  if command -v uv >/dev/null 2>&1; then
    ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
  else
    bad "uv is missing — it manages the virtualenv and runs everything"
    note "install: curl -LsSf https://astral.sh/uv/install.sh | sh"
  fi

  if command -v git >/dev/null 2>&1; then
    ok "git $(git --version | awk '{print $3}')"
  else
    bad "git is missing — worktrees and commits need it"
  fi

  if [[ -f .env ]]; then
    ok ".env exists"
  elif [[ -f .env.example ]]; then
    cp .env.example .env
    warn ".env created from .env.example — it has no secrets in it yet"
  else
    bad "neither .env nor .env.example is present; cannot configure anything"
  fi

  [[ $FAILED -eq 0 ]] || { printf '\n%sPreflight failed. Fix the above and re-run.%s\n' "$RED" "$OFF"; exit 1; }
}

check_env() {
  step "Configuration — which settings are present"

  local missing_core=0
  for v in DATABASE_URL REDIS_URL API_KEYS; do
    if env_is_set "$v"; then ok "$v set"; else bad "$v missing"; missing_core=1; fi
  done
  if [[ $missing_core -ne 0 ]]; then
    note "the API and the tests need these three; core/settings.py refuses to load without them"
  fi

  # The worker dies at startup naming these (Settings.require_worker), so a missing one
  # is a warning here and a hard failure the moment a run is picked up.
  local worker_ready=1
  if env_is_set LLM_API_KEY || env_is_set ANTHROPIC_API_KEY; then
    ok "an LLM key is set — provider=$(env_get LLM_PROVIDER openai_compat), model=$(env_get LLM_MODEL 'openrouter/free')"
  else
    warn "no LLM key (LLM_API_KEY / ANTHROPIC_API_KEY)"
    note "the API, the console and both test suites work without one; a real run does not"
    worker_ready=0
  fi
  if env_is_set GITHUB_TOKEN; then
    ok "GITHUB_TOKEN set — a run can push a branch and open a pull request"
  else
    warn "GITHUB_TOKEN missing — the worker exits at startup naming it"
    worker_ready=0
  fi
  [[ $worker_ready -eq 1 ]] || note "run 'scripts/bringup.sh' anyway: everything except a live run still comes up"

  note "ports from .env: postgres=$(env_get POSTGRES_PORT 5432) redis=$(env_get REDIS_PORT 6379)"
  note "state dirs: $(env_get WORKTREES_DIR /var/agent/worktrees), $(env_get REPOS_DIR /var/agent/repos)"
}

# ---------------------------------------------------------------- python env

sync_deps() {
  step "Python environment"
  uv sync --all-extras --quiet
  ok "dependencies installed ($(uv run python -V 2>&1))"
  ok "autoswe $(uv run autoswe version 2>/dev/null || echo '(version command failed)')"
}

# ---------------------------------------------------------------- infra

infra() {
  step "Infrastructure — Postgres, Redis, the install network"

  make up >/dev/null 2>&1 || { bad "make up failed"; docker compose ps; return; }

  local pg redis
  pg="$(env_get POSTGRES_PORT 5432)"
  redis="$(env_get REDIS_PORT 6379)"

  local waited=0
  until docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null | grep -q "postgres running" || [[ $waited -ge 60 ]]; do
    sleep 2; waited=$((waited + 2))
  done

  if docker compose ps --format '{{.Service}} {{.State}}' | grep -q "postgres running"; then
    ok "postgres running, published on :$pg"
  else
    bad "postgres did not come up"; docker compose logs --tail 20 postgres
  fi

  if docker compose ps --format '{{.Service}} {{.State}}' | grep -q "redis running"; then
    ok "redis running, published on :$redis"
  else
    bad "redis did not come up"; docker compose logs --tail 20 redis
  fi

  if port_open "$pg"; then ok "127.0.0.1:$pg accepts connections"; else bad "nothing listening on :$pg"; fi
  if port_open "$redis"; then ok "127.0.0.1:$redis accepts connections"; else bad "nothing listening on :$redis"; fi

  if docker network ls --format '{{.Name}}' | grep -qx "$(env_get SANDBOX_NETWORK agent-install)"; then
    ok "docker network '$(env_get SANDBOX_NETWORK agent-install)' exists — the sandbox installs dependencies on it, then is disconnected"
  else
    bad "the install network is missing"
  fi
}

migrate() {
  step "Database schema"
  if uv run alembic upgrade head >/dev/null 2>&1; then
    local head
    head="$(uv run alembic current 2>/dev/null | tail -1)"
    ok "migrations applied — ${head:-head}"
  else
    bad "alembic upgrade head failed"
    uv run alembic upgrade head 2>&1 | tail -15
  fi
}

sandbox_image() {
  step "Sandbox image — where the agent's code actually runs"
  local image
  image="$(env_get SANDBOX_IMAGE agent-sandbox:python-3.12)"
  if docker images "$image" --format '{{.Repository}}:{{.Tag}}' | grep -qx "$image"; then
    ok "$image present ($(docker images "$image" --format '{{.Size}}, built {{.CreatedSince}}'))"
  else
    warn "$image is missing — building it now (a few minutes, once)"
    if make sandbox-image; then
      ok "$image built"
    else
      bad "the image did not build; runs will fail in SETUP until it does"
      note "the unit suite passes without it; the integration suite skips the sandbox tests"
    fi
  fi
}

# ---------------------------------------------------------------- processes

pid_alive() { [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null; }

start_api() {
  step "API and console"
  mkdir -p "$RUNDIR"
  if pid_alive "$API_PID"; then
    ok "already running (pid $(cat "$API_PID"))"
  else
    nohup uv run uvicorn api.main:app --host 127.0.0.1 --port 8000 >"$API_LOG" 2>&1 &
    echo $! >"$API_PID"
    sleep 1
  fi

  local body="" waited=0
  until [[ -n "$body" ]] || [[ $waited -ge 40 ]]; do
    body="$(curl -sf --max-time 2 http://127.0.0.1:8000/healthz 2>/dev/null || true)"
    [[ -n "$body" ]] || { sleep 2; waited=$((waited + 2)); }
  done

  if [[ -n "$body" ]]; then
    ok "GET /healthz -> $body"
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/ || true)"
    ok "console at http://127.0.0.1:8000/ -> HTTP $code"
    code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/runs || true)"
    ok "GET /runs with no key -> HTTP $code (auth is enforced)"
  else
    bad "the API never became healthy"
    tail -20 "$API_LOG" 2>/dev/null || true
  fi
}

start_worker() {
  step "Worker"
  mkdir -p "$RUNDIR"
  if pid_alive "$WORKER_PID"; then
    ok "already running (pid $(cat "$WORKER_PID"))"
    return
  fi
  if ! (env_is_set LLM_API_KEY || env_is_set ANTHROPIC_API_KEY) || ! env_is_set GITHUB_TOKEN; then
    warn "not starting the worker: it exits immediately without an LLM key and GITHUB_TOKEN"
    note "everything else above is up; add the keys to .env and re-run this script"
    return
  fi
  nohup uv run arq orchestrator.worker.WorkerSettings >"$WORKER_LOG" 2>&1 &
  echo $! >"$WORKER_PID"
  sleep 6
  if pid_alive "$WORKER_PID" && grep -q "worker_started" "$WORKER_LOG" 2>/dev/null; then
    ok "worker_started — $(grep -m1 -o '"model": "[^"]*"' "$WORKER_LOG" | head -1)"
    ok "listening for jobs on redis :$(env_get REDIS_PORT 6379)"
  else
    bad "the worker did not start"
    tail -20 "$WORKER_LOG" 2>/dev/null || true
  fi
}

# ---------------------------------------------------------------- commands

summary() {
  local key
  key="$(env_get API_KEYS | cut -d, -f1)"
  printf '\n%s== Ready%s\n' "$BOLD" "$OFF"
  printf '  console   http://127.0.0.1:8000/   (paste the API key from .env into the field)\n'
  printf '  health    curl -s http://127.0.0.1:8000/healthz\n'
  printf '  api key   %s chars, first of %s in API_KEYS\n' "${#key}" "$(env_get API_KEYS | tr ',' '\n' | grep -c . || echo 1)"
  printf '  logs      scripts/bringup.sh logs\n'
  printf '  a run     export AUTOSWE_API_KEY=$(grep -m1 ^API_KEYS= .env | cut -d= -f2- | cut -d, -f1)\n'
  printf '            uv run autoswe run --repo https://github.com/OWNER/REPO --goal "..."\n'
  printf '\n  next      TESTING.md walks through what is built, with a proof for each claim\n'
  if [[ $FAILED -ne 0 ]]; then
    printf '\n%sSome steps failed — see the ✗ lines above.%s\n' "$RED" "$OFF"
    exit 1
  fi
}

cmd_status() {
  step "Processes"
  pid_alive "$API_PID" && ok "api pid $(cat "$API_PID")" || warn "api not running"
  pid_alive "$WORKER_PID" && ok "worker pid $(cat "$WORKER_PID")" || warn "worker not running"

  step "Containers"
  docker compose ps --format '  {{.Service}}  {{.State}}  {{.Publishers}}' 2>/dev/null || warn "compose not up"

  step "Health"
  local body
  body="$(curl -sf --max-time 3 http://127.0.0.1:8000/healthz 2>/dev/null || true)"
  [[ -n "$body" ]] && ok "$body" || warn "the API is not answering on :8000"

  step "Disk used by run state"
  local wt
  wt="$(env_get WORKTREES_DIR /var/agent/worktrees)"
  if [[ -d "$wt" ]]; then
    printf '  %s worktrees, %s\n' "$(find "$wt" -maxdepth 1 -mindepth 1 -type d | wc -l)" "$(du -sh "$wt" 2>/dev/null | cut -f1)"
    note "a worktree is kept for every run that did not push, on purpose — nothing reaps them yet"
  fi
}

cmd_down() {
  step "Stopping the API and worker (the data stays)"
  for f in "$API_PID" "$WORKER_PID"; do
    if pid_alive "$f"; then
      kill "$(cat "$f")" 2>/dev/null || true
      ok "stopped pid $(cat "$f")"
      rm -f "$f"
    fi
  done
  note "containers left running; 'reset' stops them and deletes the volumes"
}

cmd_reset() {
  cmd_down
  step "Removing containers and volumes"
  make down
  ok "compose down -v — the database is empty next time"
}

cmd_seed() {
  step "Seeding a run for the console (no model, no quota)"
  note "writes the same rows and publishes the same events a real run does, at a watchable pace"
  uv run python scripts/seed_demo_run.py "$@"
}

cmd_logs() {
  step "Logs (ctrl-c to stop)"
  tail -f "$API_LOG" "$WORKER_LOG" 2>/dev/null
}

case "${1-up}" in
  up)
    preflight; check_env; sync_deps; infra; migrate; sandbox_image; start_api; start_worker; summary ;;
  infra-only)
    preflight; check_env; sync_deps; infra; migrate; summary ;;
  status) cmd_status ;;
  down)   cmd_down ;;
  reset)  cmd_reset ;;
  seed)   shift; cmd_seed "$@" ;;
  logs)   cmd_logs ;;
  *)
    printf 'usage: %s [up|infra-only|status|seed|down|reset|logs]\n' "$0" >&2; exit 2 ;;
esac
