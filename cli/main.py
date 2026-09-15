"""CLI entrypoint. Phase 1 adds ``run`` and ``status``; Phase 2 adds ``watch``,
``answer`` and ``cancel``; Phase 3 adds ``approve`` and ``reject``."""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator
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


def _frames(lines: Iterable[str]) -> Iterator[tuple[str, str, str]]:
    """Yield ``(id, type, data)`` per server-sent event.

    ``iter_lines`` already strips the CRLF the wire format uses, so a blank line is the
    frame boundary. Fields we do not use are ignored, as the spec requires.
    """
    event_id, type_, data = "", "message", ""
    for line in lines:
        if line:
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "id":
                event_id = value
            elif field == "event":
                type_ = value
            elif field == "data":
                data = f"{data}\n{value}" if data else value
            continue
        if type_ or data:
            yield event_id, type_, data
        event_id, type_, data = "", "message", ""


def _describe(type_: str, p: dict[str, Any]) -> str:
    """One line per event, the same summaries the console shows."""
    match type_:
        case "phase_changed":
            return f"{p.get('phase', '')}{f' ({p["tasks"]} tasks)' if p.get('tasks') else ''}"
        case "agent_started":
            return f"{p.get('agent', '')}{f' on {p["task_id"]}' if p.get('task_id') else ''}"
        case "agent_finished":
            return f"error: {p['error']}" if p.get("error") else "finished"
        case "tool_call":
            return f"{p.get('name', '')}{' — failed' if p.get('is_error') else ''}"
        case "test_report":
            return f"{'passed' if p.get('passed') else 'failed'} — {p.get('total')} tests, {p.get('failed')} failing"  # noqa: E501
        case "awaiting_input":
            return " · ".join(p.get("questions") or [])
        case "run_finished":
            return f"{p.get('status', '')} · ${float(p.get('cost_usd') or 0):.4f}"
        case _:
            return str(p.get("pr_url") or p.get("message") or "")


@app.command()
def watch(
    run_id: Annotated[str, typer.Argument(help="Run id printed by `autoswe run`.")],
    api: Annotated[str, typer.Option(help="Control-plane base URL.")] = DEFAULT_API,
    key: Annotated[str | None, typer.Option(help="API key (or AUTOSWE_API_KEY).")] = None,
) -> None:
    """Follow a run's events until it finishes. Exits non-zero if the run did not pass."""
    import json

    import httpx

    last_id = ""
    while True:
        headers = {"Last-Event-ID": last_id} if last_id else {}
        try:
            with (
                _client(api, key) as client,
                client.stream(
                    "GET", f"/runs/{run_id}/events", headers=headers, timeout=None
                ) as response,
            ):
                if response.status_code != 200:
                    response.read()
                    _check(response)
                for event_id, type_, data in _frames(response.iter_lines()):
                    if event_id:
                        last_id = event_id  # so a reconnect resumes rather than repeats
                    if type_ == "keepalive":
                        continue
                    payload = json.loads(data) if data else {}
                    typer.echo(f"{type_:16} {_describe(type_, payload)}")
                    if type_ == "run_finished":
                        raise typer.Exit(0 if payload.get("status") == "done" else 1)
        except httpx.TransportError:
            time.sleep(1.0)  # the stream dropped mid-run; pick up from the cursor
            continue
        return  # the server closed a finished run's replay


@app.command()
def answer(
    run_id: Annotated[str, typer.Argument(help="Run id printed by `autoswe run`.")],
    text: Annotated[str, typer.Argument(help="The answer to the run's open questions.")],
    api: Annotated[str, typer.Option(help="Control-plane base URL.")] = DEFAULT_API,
    key: Annotated[str | None, typer.Option(help="API key (or AUTOSWE_API_KEY).")] = None,
) -> None:
    """Answer a run parked on an open question, so it can carry on."""
    with _client(api, key) as client:
        _check(client.post(f"/runs/{run_id}/answer", json={"text": text}))
    typer.echo("accepted")


@app.command()
def approve(
    run_id: Annotated[str, typer.Argument(help="Run id printed by `autoswe run`.")],
    tool_call_id: Annotated[str, typer.Argument(help="From the awaiting_input event.")],
    api: Annotated[str, typer.Option(help="Control-plane base URL.")] = DEFAULT_API,
    key: Annotated[str | None, typer.Option(help="API key (or AUTOSWE_API_KEY).")] = None,
) -> None:
    """Let a tool call the run is parked on proceed."""
    with _client(api, key) as client:
        _check(client.post(f"/runs/{run_id}/approve", json={"tool_call_id": tool_call_id}))
    typer.echo("approved")


@app.command()
def reject(
    run_id: Annotated[str, typer.Argument(help="Run id printed by `autoswe run`.")],
    tool_call_id: Annotated[str, typer.Argument(help="From the awaiting_input event.")],
    reason: Annotated[str, typer.Argument(help="Why. The model is told, so be specific.")],
    api: Annotated[str, typer.Option(help="Control-plane base URL.")] = DEFAULT_API,
    key: Annotated[str | None, typer.Option(help="API key (or AUTOSWE_API_KEY).")] = None,
) -> None:
    """Refuse the call. The reason comes back to the model as the tool's result."""
    with _client(api, key) as client:
        _check(
            client.post(
                f"/runs/{run_id}/reject",
                json={"tool_call_id": tool_call_id, "reason": reason},
            )
        )
    typer.echo("rejected")


@app.command()
def cancel(
    run_id: Annotated[str, typer.Argument(help="Run id printed by `autoswe run`.")],
    api: Annotated[str, typer.Option(help="Control-plane base URL.")] = DEFAULT_API,
    key: Annotated[str | None, typer.Option(help="API key (or AUTOSWE_API_KEY).")] = None,
) -> None:
    """Ask a run to stop at the next node or tool call, whichever comes first."""
    with _client(api, key) as client:
        _check(client.post(f"/runs/{run_id}/cancel"))
    typer.echo("cancel requested")


if __name__ == "__main__":
    app()
