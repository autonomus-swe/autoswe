.RECIPEPREFIX := >
.PHONY: up down migrate lint fmt type test test-int check sandbox-image

up:
> docker compose up -d --wait postgres redis

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
> @echo "Phase 1: docker build -t agent-sandbox:python-3.12 sandbox/images/python"
