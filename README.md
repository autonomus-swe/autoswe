# autoswe — Phase 0 (foundations)

This directory is the project root produced by [PHASE-0-foundations.md](docs/PHASE-0-foundations.md).
Later phases build in this same tree.

```bash
uv sync --all-extras          # deps (installs the dev group too)
cp .env.example .env
make up && make migrate       # postgres + redis, then alembic upgrade head
                              # (set POSTGRES_PORT / REDIS_PORT in .env if 5432/6379 are taken, and match DATABASE_URL/REDIS_URL)
make check                    # ruff + mypy + unit tests
make test-int                 # integration tests via testcontainers (needs Docker)
uv run autoswe config         # prints the non-secret settings; exits 2 naming any missing variable
```

Manual walkthrough of every Phase 0 capability: [MANUAL-TESTING.md](MANUAL-TESTING.md).
