"""The eval recorder. Small, but it writes the numbers this project quotes about itself."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals import record

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _redirect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTOSWE_EVAL_RESULTS", str(tmp_path / "results"))


def test_a_row_is_appended_not_overwritten(tmp_path: Path) -> None:
    """How the agent did on a scenario over time is more useful than its last attempt."""
    record.append("m3", {"scenario": "a-off-by-one", "cost_usd": 0.12})
    path = record.append("m3", {"scenario": "a-off-by-one", "cost_usd": 0.09})

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["cost_usd"] for r in rows] == [0.12, 0.09]
    assert record.read("m3") == rows


def test_every_row_says_when(tmp_path: Path) -> None:
    record.append("m3", {"scenario": "c-impossible"})
    (written,) = record.read("m3")
    assert written["at"].startswith("20"), "a number with no date cannot be compared to a later one"


def test_reading_a_file_that_was_never_written_is_not_an_error() -> None:
    assert record.read("nothing-ran-yet") == []
