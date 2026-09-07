"""CLI entrypoint. Phase 0 exposes only ``version`` and ``config``; later phases add run/watch."""

from __future__ import annotations

import typer

app = typer.Typer(help="autoswe: autonomous software engineering agent", no_args_is_help=True)


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


if __name__ == "__main__":
    app()
