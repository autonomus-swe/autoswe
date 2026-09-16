"""Append one row per scenario run to a JSONL file.

These are the numbers the project quotes about itself, so they are written by the run that
produced them and never by hand. A row is appended, not rewritten: the history of how the
agent did on a scenario is more useful than its most recent attempt, and the file is small.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RESULTS = Path(__file__).parent / "results"


def append(name: str, row: dict[str, Any]) -> Path:
    """Add ``row`` to ``evals/results/{name}.jsonl`` and return the path.

    ``AUTOSWE_EVAL_RESULTS`` redirects the directory, so a test can prove this works
    without writing into the repository.
    """
    directory = Path(os.environ.get("AUTOSWE_EVAL_RESULTS", RESULTS))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.jsonl"
    stamped = {"at": datetime.now(UTC).isoformat(timespec="seconds"), **row}
    with path.open("a") as f:
        f.write(json.dumps(stamped, sort_keys=False) + "\n")
    return path


def read(name: str) -> list[dict[str, Any]]:
    directory = Path(os.environ.get("AUTOSWE_EVAL_RESULTS", RESULTS))
    path = directory / f"{name}.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
