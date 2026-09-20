"""Liveness and readiness probes."""


def healthz() -> dict[str, str]:
    return {"status": "ok"}


def readyz(database_up: bool, cache_up: bool) -> dict[str, object]:
    checks = {"database": database_up, "cache": cache_up}
    return {"status": "ok" if all(checks.values()) else "degraded", "checks": checks}
