"""CLI entrypoint. Phase 1 adds ``run`` and ``status``; Phase 2 adds ``watch``."""

from __future__ import annotations

import os
from typing import Annotated, Any

import typer

app = typer.Typer(help="autoswe: autonomous software engineering agent", no_args_is_help=True)

DEFAULT_API = os.environ.get("AUTOSWE_API", "http://127.0.0.1:8000")


def _client(api: str, key: str | None) -> Any:
    import httpx

    token = key or os.environ.get("AUTOSWE_API_KEY", "")
    if not token:
        typer.echo("error: pass --key or set AUTOSWE_API_KEY", err=True)
        raise typer.Exit(2)
    return httpx.Client(base_url=api.rstrip("/"), headers={"X-API-Key": token}, timeout=30.0)


def _check(response: Any) -> Any:
    import httpx

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.json().get("detail", "")
        except Exception:  # error bodies are not always JSON
            detail = e.response.text[:200]
        typer.echo(f"error: {e.response.status_code} {detail}", err=True)
        raise typer.Exit(1) from e
    return response.json()


@app.command()
def version() -> None:
    """Print the package version."""
    from importlib.metadata import version as pkg_version

    typer.echo(pkg_version("autoswe"))


@app.command()
def config() -> None:
    """Load settings and print the non-secret view. Exits 2 if anything required is missing."""
    from core.settings import get_settings

    for key, value in get_settings().public_dict().items():
        typer.echo(f"{key:20} {value}")


@app.command()
def run(
    repo: Annotated[str, typer.Option(help="https://github.com/owner/name")],
    goal: Annotated[str, typer.Option(help="What the agent should accomplish.")],
    base: Annotated[str, typer.Option(help="Branch to start from.")] = "main",
    api: Annotated[str, typer.Option(help="Control-plane base URL.")] = DEFAULT_API,
    key: Annotated[str | None, typer.Option(help="API key (or AUTOSWE_API_KEY).")] = None,
) -> None:
    """Start a run and print its id."""
    with _client(api, key) as client:
        body = _check(
            client.post("/runs", json={"repo_url": repo, "goal": goal, "base_branch": base})
        )
    typer.echo(body["run_id"])


@app.command()
def status(
    run_id: Annotated[str, typer.Argument(help="Run id printed by `autoswe run`.")],
    api: Annotated[str, typer.Option(help="Control-plane base URL.")] = DEFAULT_API,
    key: Annotated[str | None, typer.Option(help="API key (or AUTOSWE_API_KEY).")] = None,
) -> None:
    """Print one run's summary."""
    with _client(api, key) as client:
        body = _check(client.get(f"/runs/{run_id}"))
    for field in ("phase", "status", "cost_usd", "pr_url", "error", "work_branch", "updated_at"):
        value = body.get(field)
        if value not in (None, ""):
            typer.echo(f"{field:12} {value}")


if __name__ == "__main__":
    app()
