"""CLI entrypoint. Phase 1 adds ``run`` and ``status``; Phase 2 adds ``watch``,
``answer`` and ``cancel``; Phase 3 adds ``approve`` and ``reject``; Phase 4 adds
``artifacts``."""

from __future__ import annotations

import json
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


def _raise(response: Any) -> None:
    """Turn an HTTP error into a CLI error, with the server's detail if it sent one.

    Split out from `_check` because not every endpoint returns JSON — the `diff` artifact
    is served as text/plain so it can be piped to `git apply`, and calling `.json()` on it
    would report a parse error instead of the 404 the server actually sent.
    """
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


def _check(response: Any) -> Any:
    _raise(response)
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
            verdict = (
                "baseline" if p.get("baseline") else ("passed" if p.get("passed") else "failed")
            )
            excused = "".join(
                f" · {len(p[key])} {label}"
                for key, label in (("flaky", "flaky"), ("pre_existing", "pre-existing"))
                if p.get(key)
            )
            return f"{verdict} — {p.get('total')} tests, {p.get('failed')} failing{excused}"
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


@app.command()
def artifacts(
    run_id: Annotated[str, typer.Argument(help="Run id printed by `autoswe run`.")],
    # Not an enumerated list: the kinds a run writes grow with the pipeline, and a list
    # here would go stale silently. Running without one prints what this run actually has.
    kind: Annotated[
        str | None, typer.Argument(help="Which artifact. Omit to list what the run wrote.")
    ] = None,
    api: Annotated[str, typer.Option(help="Control-plane base URL.")] = DEFAULT_API,
    key: Annotated[str | None, typer.Option(help="API key (or AUTOSWE_API_KEY).")] = None,
) -> None:
    """List a run's artifacts, or print one of them.

    Everything the run learned that was too large for an event. The listing carries sizes
    rather than content because a `diff` can be megabytes and deciding whether to fetch one
    should not require fetching it.
    """
    with _client(api, key) as client:
        if kind is None:
            rows = _check(client.get(f"/runs/{run_id}/artifacts"))
            if not rows:
                typer.echo("no artifacts")
                return
            for row in rows:
                typer.echo(f"{row['kind']:16} {row['size']:>9}  {row['created_at']}")
            return
        response = client.get(f"/runs/{run_id}/artifacts/{kind}")
        _raise(response)
    # `diff` is served as text/plain so it can go straight to `git apply`; everything else
    # is JSON, printed as JSON so it can go straight to `jq`.
    if response.headers.get("content-type", "").startswith("text/plain"):
        typer.echo(response.text)
    else:
        typer.echo(json.dumps(response.json(), indent=2))


@app.command()
def eval(
    suite: Annotated[str, typer.Option(help="Which suite in evals/tasks/.")] = "private",
    tags: Annotated[str, typer.Option(help="Comma-separated; runs only tasks with one.")] = "",
    concurrency: Annotated[int, typer.Option(help="Tasks in flight at once.")] = 1,
    provider: Annotated[str | None, typer.Option(help="Override the run provider.")] = None,
    api: Annotated[str, typer.Option(help="Control-plane base URL.")] = DEFAULT_API,
    key: Annotated[str | None, typer.Option(help="API key (or AUTOSWE_API_KEY).")] = None,
    results: Annotated[str | None, typer.Option(help="Results file stem.")] = None,
) -> None:
    """Run an eval suite and print the table.

    Exits non-zero when anything went unresolved, so this is usable in CI without a
    wrapper that greps the output. A task with no verify command is unverifiable rather
    than unresolved and does not fail the command — see `docs/evals.md`.
    """
    import asyncio
    from dataclasses import asdict

    from evals import report
    from evals.run import run_suite
    from evals.suite import load

    token = key or os.environ.get("AUTOSWE_API_KEY", "")
    if not token:
        typer.echo("error: pass --key or set AUTOSWE_API_KEY", err=True)
        raise typer.Exit(2)

    chosen = load(suite).tagged(tuple(t for t in tags.split(",") if t))
    if not chosen.tasks:
        typer.echo(f"error: no tasks in suite {suite!r} matching {tags!r}", err=True)
        raise typer.Exit(2)

    rows = asyncio.run(
        run_suite(
            chosen,
            api=api,
            key=token,
            concurrency=concurrency,
            provider=provider,
            results_name=results,
        )
    )
    typer.echo(report.render([asdict(r) for r in rows], title=f"Suite: {chosen.name}"))
    raise typer.Exit(0 if all(r.resolved is not False for r in rows) else 1)


@app.command()
def mcp() -> None:
    """Serve the control plane over MCP on stdin and stdout.

    The same thing the `autoswe-mcp` console script does. That one exists separately
    because an editor's MCP configuration runs a single command and typer prints its own
    diagnostics to stdout — which on this transport is the protocol.
    """
    from cli.mcp import main as serve

    serve()
