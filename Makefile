.RECIPEPREFIX := >
.PHONY: up down migrate lint fmt type test test-int test-e2e check sandbox-image sandbox-images egress-proxy-image egress-up api worker

up:
> docker compose up -d --wait postgres redis
> docker network inspect agent-install >/dev/null 2>&1 || docker network create agent-install

down:
> docker compose down -v

migrate:
> uv run alembic upgrade head

lint:
> uv run ruff check .
> uv run ruff format --check .

fmt:
> uv run ruff format .
> uv run ruff check --fix .

type:
> uv run mypy .

test:
> uv run pytest -m unit

test-int:
> uv run pytest -m integration

check: lint type test

# The python image alone is what the integration suite needs, so this stays the cheap
# target CI and bringup call. `sandbox-images` builds the set a real run may select from.
# `--pull` on every image build, and it is not hygiene.
#
# The Dockerfiles pin `bookworm` so a Debian major release cannot move under a tested
# image. What that does not pin is the *patch* level: a machine that pulled
# `python:3.12-slim-bookworm` months ago keeps building from that cached layer forever,
# and apt inside it sees the snapshot it was built with.
#
# Measured: an image built from a stale base carried libpcre2 10.42-1 instead of
# 10.42-1+deb12u1, and `git clone` inside it died with
# `fatal: fetch-pack: invalid index-pack output` — git's index-pack segfaulting in a
# library it links. Rebuilding with `--pull` fixed it. Nothing said the image was stale;
# the symptom was a git failure with no mention of a library.
sandbox-image:
> docker build --pull -t agent-sandbox:python-3.12 sandbox/images/python

egress-proxy-image:
> docker build --pull -t agent-egress-proxy proxy

# The network has to exist before compose can attach the proxy to it (compose declares it
# `external`), and the worker only creates it on its first run — so this creates it, with
# the `internal` flag that is the actual enforcement. Idempotent.
egress-up: egress-proxy-image
> docker network inspect agent-egress >/dev/null 2>&1 || docker network create --internal agent-egress
> docker compose --profile proxy up -d
> @echo 'set EGRESS_PROXY_URL=http://agent-egress-proxy:8888 in .env to enforce it'

sandbox-images: sandbox-image
> docker build --pull -t agent-sandbox:node-20 sandbox/images/node
> docker build --pull -t agent-sandbox:go-1.23 sandbox/images/go

api:
> uv run uvicorn api.main:app --host 127.0.0.1 --port 8000

worker:
> uv run arq orchestrator.worker.WorkerSettings

test-e2e:
> uv run pytest -m e2e tests/e2e/test_m1_github.py

# Step 5.10, as one command. The scale run needs a large checkout, a model that both
# caches and writes working code, and a repository whose size is the point — reconstructing
# that from the phase document by hand is how a run gets made against the wrong thing and
# the number quietly means something else.
#
# SCALE_REPO defaults to a Django clone because `docs/numbers.md` already carries its
# host-side figures, so a run against it can be compared rather than just recorded. Any
# checkout in the 2 000-5 000 file range the phase document asks for will do:
#   make scale-run SCALE_REPO=~/src/sympy
#
# Cloned, never used in place — a run creates branches and resets, and the fixture does
# the clone so this cannot touch the original.
# A model id and the endpoint that serves it are one setting, not two. Overriding
# LLM_MODEL alone leaves LLM_BASE_URL at whatever `.env` says — which is how the first
# version of this target sent an OpenRouter model name to a local Ollama and got a 404
# after starting a sandbox and indexing 3 043 files. They move together here so that
# cannot happen.
SCALE_REPO ?= $(HOME)/.autoswe-scale/django
SCALE_MODEL ?= nvidia/nemotron-3-ultra-550b-a55b:free
SCALE_BASE_URL ?= https://openrouter.ai/api/v1
# The key is never a default, never written here, and never echoed. It is looked for in
# the environment first and then in a dotenv file, so the usual case is `make scale-run`
# with nothing after it — a secret pasted on a command line ends up in shell history, and
# telling someone to paste one is a worse default than reading the file they already have.
SCALE_ENV_FILE ?= .env.openrouter.bak

# Resolved inside the recipe rather than as a make variable, so no target that does not
# need the key ever reads it, and `make -n` cannot print it. Every recipe using this is
# `@`-prefixed for the same reason: make echoes commands by default, which puts the key on
# stdout and into any CI log that captures it. The first version of this target did.
scale_key = $${SCALE_API_KEY:-$$(test -f $(SCALE_ENV_FILE) && sed -n 's/^LLM_API_KEY=//p' $(SCALE_ENV_FILE) | head -1)}

# Fails in a second on a key or model that will not work, rather than after SETUP has
# built a sandbox and parsed the repository. The run is the expensive part; finding out
# the configuration was wrong should not be.
scale-preflight:
> @key="$(scale_key)"; \
> test -n "$$key" || { \
>   echo "No API key. Put it in $(SCALE_ENV_FILE) as LLM_API_KEY=..., export SCALE_API_KEY,"; \
>   echo "or pass it: make $(MAKECMDGOALS) SCALE_API_KEY=sk-or-..."; \
>   exit 1; }; \
> curl -sf -o /dev/null -X POST $(SCALE_BASE_URL)/chat/completions \
>   -H "Authorization: Bearer $$key" -H 'Content-Type: application/json' \
>   -d '{"model":"$(SCALE_MODEL)","messages":[{"role":"user","content":"ok"}],"max_tokens":1}' \
>   || { echo "$(SCALE_MODEL) is not reachable at $(SCALE_BASE_URL) with this key."; \
>        echo "A 404 means the model id is wrong for that endpoint; 429 means the daily free allowance is spent."; \
>        exit 1; }; \
> echo "preflight ok: $(SCALE_MODEL) at $(SCALE_BASE_URL)"

scale-run: scale-preflight
> @key="$(scale_key)"; AUTOSWE_SCALE_REPO=$(SCALE_REPO) LLM_BASE_URL=$(SCALE_BASE_URL) \
> LLM_API_KEY="$$key" LLM_MODEL=$(SCALE_MODEL) \
> uv run pytest -m e2e tests/e2e/test_m5_scale.py -q -s

# The ablation the same step prescribes: the identical run with the ranked map replaced by
# the v1 tree. Two runs, one variable, which is the only way the v1-vs-v2 question in
# `docs/numbers.md` can be answered rather than argued.
scale-ablation: scale-preflight
> @key="$(scale_key)"; AUTOSWE_SCALE_REPO=$(SCALE_REPO) LLM_BASE_URL=$(SCALE_BASE_URL) \
> LLM_API_KEY="$$key" LLM_MODEL=$(SCALE_MODEL) REPO_MAP_VERSION=v1 \
> uv run pytest -m e2e tests/e2e/test_m5_scale.py -q -s

# both end-to-end tests; costs two runs of quota and can trip a per-minute rate limit
test-e2e-all:
> uv run pytest -m e2e tests/e2e
