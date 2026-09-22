"""CLI entrypoint. Phase 1 adds ``run`` and ``status``; Phase 2 adds ``watch``,
``answer`` and ``cancel``; Phase 3 adds ``approve`` and ``reject``; Phase 4 adds
``artifacts``."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator
from typing import Annotated, Any

import typer

from cli import config as cli_config

app = typer.Typer(help="autoswe: autonomous software engineering agent", no_args_is_help=True)

# `None` means "no flag given", which `cli.config.load` needs in order to tell a flag
# apart from a default. Typer shows the resolved value in `--help` through the help text
# rather than through a default, so nobody reads a placeholder as a promise.
API_HELP = "Control-plane base URL (or AUTOSWE_API, or ~/.config/autoswe/config.toml)."
KEY_HELP = "API key (or AUTOSWE_API_KEY, or ~/.config/autoswe/config.toml)."
DEFAULT_API = cli_config.DEFAULT_API


def _client(api: str | None, key: str | None) -> Any:
    import httpx

    settings = cli_config.load(api=api, key=key)
    if not settings.key:
        typer.echo(
            'error: pass --key, set AUTOSWE_API_KEY, or put `key = "..."` in '
            f"{cli_config.CONFIG_PATH}",
            err=True,
        )
        raise typer.Exit(2)
    return httpx.Client(base_url=settings.api, headers={"X-API-Key": settings.key}, timeout=30.0)


def _emit(body: Any, as_json: bool, lines: Iterable[str]) -> None:
    """Print a read command's result, as JSON or as the human rendering.

    One helper rather than an `if` in every command: `--json` that worked on four commands
    out of six would be worse than none, because a script cannot tell which without
    trying.
    """
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    for line in lines:
        typer.echo(line)


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
    budget: Annotated[float | None, typer.Option(help="Dollar ceiling for this run.")] = None,
    unattended: Annotated[
        bool, typer.Option(help="Nobody is watching: refuse approvals rather than park.")
    ] = False,
    provider: Annotated[str | None, typer.Option(help="LLM provider for this run.")] = None,
    upstream: Annotated[
        str | None,
        typer.Option(help="owner/repo to open the PR on, when --repo is your fork."),
    ] = None,
    follow: Annotated[
        bool, typer.Option(help="Stream events and exit with the run's status.")
    ] = False,
    api: Annotated[str | None, typer.Option(help=API_HELP)] = None,
    key: Annotated[str | None, typer.Option(help=KEY_HELP)] = None,
) -> None:
    """Start a run and print its id.

    With `--follow`, stream its events and exit with the run's own outcome: 0 done,
    1 failed, 2 awaiting input. The third is not a failure — it means the run is waiting
    for you — and a script that treated it as one would give up on a question it could
    have answered.
    """
    body: dict[str, Any] = {"repo_url": repo, "goal": goal, "base_branch": base}
    if budget is not None:
        body["budget"] = {"max_usd": budget}
    if unattended:
        body["unattended"] = True
    if provider:
        body["provider"] = provider
    if upstream:
        body["upstream"] = upstream

    with _client(api, key) as client:
        started = _check(client.post("/runs", json=body))
    run_id = started["run_id"]
    typer.echo(run_id)
    if follow:
        _follow(run_id, api, key)


@app.command()
def status(
    run_id: Annotated[str, typer.Argument(help="Run id printed by `autoswe run`.")],
    json_out: Annotated[bool, typer.Option("--json", help="The server's JSON, verbatim.")] = False,
    api: Annotated[str | None, typer.Option(help=API_HELP)] = None,
    key: Annotated[str | None, typer.Option(help=KEY_HELP)] = None,
) -> None:
    """Print one run's summary."""
    with _client(api, key) as client:
        body = _check(client.get(f"/runs/{run_id}"))
    fields = ("phase", "status", "cost_usd", "pr_url", "error", "work_branch", "updated_at")
    _emit(
        body,
        json_out,
        (f"{f:12} {body[f]}" for f in fields if body.get(f) not in (None, "")),
    )


@app.command(name="list")
def list_runs(
    limit: Annotated[int, typer.Option(help="How many, newest first.")] = 20,
    json_out: Annotated[bool, typer.Option("--json", help="The server's JSON, verbatim.")] = False,
    api: Annotated[str | None, typer.Option(help=API_HELP)] = None,
    key: Annotated[str | None, typer.Option(help=KEY_HELP)] = None,
) -> None:
    """Recent runs, newest first — for picking up where you left off."""
    with _client(api, key) as client:
        rows = _check(client.get("/runs", params={"limit": limit}))
    _emit(
        rows,
        json_out,
        (f"{r['run_id']}  {r['status']:14} {r['phase']:10} {r['goal'][:60]}" for r in rows),
    )


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


AWAITING_INPUT_EXIT = 2


def _follow(run_id: str, api: str | None, key: str | None, *, stop_on_input: bool = False) -> None:
    """Stream a run's events, printing each, and exit with the run's own outcome.

    One implementation for `watch` and for `run --follow`, which differ in a single
    question: whether a run that parks for input is something to keep waiting through or
    something to hand back. `watch` was written for a second terminal, where answering
    happens elsewhere and the stream should carry on; `--follow` is the foreground of the
    command that started the run, so it returns control with exit 2.
    """
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
                    if type_ == "awaiting_input" and stop_on_input:
                        typer.echo(
                            f"the run is waiting for you: autoswe answer {run_id} '<your answer>'",
                            err=True,
                        )
                        raise typer.Exit(AWAITING_INPUT_EXIT)
                    if type_ == "run_finished":
                        raise typer.Exit(0 if payload.get("status") == "done" else 1)
        except httpx.TransportError:
            time.sleep(1.0)  # the stream dropped mid-run; pick up from the cursor
            continue
        return  # the server closed a finished run's replay


@app.command()
def watch(
    run_id: Annotated[str, typer.Argument(help="Run id printed by `autoswe run`.")],
    api: Annotated[str | None, typer.Option(help=API_HELP)] = None,
    key: Annotated[str | None, typer.Option(help=KEY_HELP)] = None,
) -> None:
    """Follow a run's events until it finishes. Exits non-zero if the run did not pass."""
    _follow(run_id, api, key)


@app.command()
def answer(
    run_id: Annotated[str, typer.Argument(help="Run id printed by `autoswe run`.")],
    text: Annotated[str, typer.Argument(help="The answer to the run's open questions.")],
    api: Annotated[str | None, typer.Option(help=API_HELP)] = None,
    key: Annotated[str | None, typer.Option(help=KEY_HELP)] = None,
) -> None:
    """Answer a run parked on an open question, so it can carry on."""
    with _client(api, key) as client:
        _check(client.post(f"/runs/{run_id}/answer", json={"text": text}))
    typer.echo("accepted")


@app.command()
def approve(
    run_id: Annotated[str, typer.Argument(help="Run id printed by `autoswe run`.")],
    tool_call_id: Annotated[str, typer.Argument(help="From the awaiting_input event.")],
    api: Annotated[str | None, typer.Option(help=API_HELP)] = None,
    key: Annotated[str | None, typer.Option(help=KEY_HELP)] = None,
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
    api: Annotated[str | None, typer.Option(help=API_HELP)] = None,
    key: Annotated[str | None, typer.Option(help=KEY_HELP)] = None,
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
    api: Annotated[str | None, typer.Option(help=API_HELP)] = None,
    key: Annotated[str | None, typer.Option(help=KEY_HELP)] = None,
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
    json_out: Annotated[bool, typer.Option("--json", help="The server's JSON, verbatim.")] = False,
    api: Annotated[str | None, typer.Option(help=API_HELP)] = None,
    key: Annotated[str | None, typer.Option(help=KEY_HELP)] = None,
) -> None:
    """List a run's artifacts, or print one of them.

    Everything the run learned that was too large for an event. The listing carries sizes
    rather than content because a `diff` can be megabytes and deciding whether to fetch one
    should not require fetching it.
    """
    with _client(api, key) as client:
        if kind is None:
            rows = _check(client.get(f"/runs/{run_id}/artifacts"))
            if not rows and not json_out:
                typer.echo("no artifacts")
                return
            _emit(
                rows,
                json_out,
                (f"{r['kind']:16} {r['size']:>9}  {r['created_at']}" for r in rows),
            )
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
    api: Annotated[str | None, typer.Option(help=API_HELP)] = None,
    key: Annotated[str | None, typer.Option(help=KEY_HELP)] = None,
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

    resolved = cli_config.load(api=api, key=key)
    if not resolved.key:
        typer.echo("error: pass --key or set AUTOSWE_API_KEY", err=True)
        raise typer.Exit(2)

    chosen = load(suite).tagged(tuple(t for t in tags.split(",") if t))
    if not chosen.tasks:
        typer.echo(f"error: no tasks in suite {suite!r} matching {tags!r}", err=True)
        raise typer.Exit(2)

    rows = asyncio.run(
        run_suite(
            chosen,
            api=resolved.api,
            key=resolved.key,
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
