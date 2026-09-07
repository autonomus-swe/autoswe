.RECIPEPREFIX := >
.PHONY: up down migrate lint fmt type test test-int test-e2e check sandbox-image api worker

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

sandbox-image:
> docker build -t agent-sandbox:python-3.12 sandbox/images/python

api:
> uv run uvicorn api.main:app --host 127.0.0.1 --port 8000

worker:
> uv run arq orchestrator.worker.WorkerSettings

test-e2e:
> uv run pytest -m e2e tests/e2e
