"""M1 against a real GitHub fixture repository through the control plane.

Needs AUTOSWE_FIXTURE_REPO (an https://github.com/... URL you can push to), GITHUB_TOKEN
with Contents and Pull requests read/write on it, a running API and worker, and
AUTOSWE_API_KEY. Skipped otherwise.
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

from tests.e2e.conftest import GOAL

pytestmark = pytest.mark.e2e

TIMEOUT_S = 15 * 60
POLL_S = 10


@pytest.fixture
def api_client() -> httpx.Client:
    repo = os.environ.get("AUTOSWE_FIXTURE_REPO")
    key = os.environ.get("AUTOSWE_API_KEY")
    if not repo or not key:
        pytest.skip("set AUTOSWE_FIXTURE_REPO and AUTOSWE_API_KEY to run this test")
    base = os.environ.get("AUTOSWE_API", "http://127.0.0.1:8000")
    client = httpx.Client(base_url=base, headers={"X-API-Key": key}, timeout=30.0)
    try:
        client.get("/healthz").raise_for_status()
    except Exception:
        pytest.skip(f"no control plane at {base}; start `make api` and `make worker`")
    return client


def test_m1_through_the_api(api_client: httpx.Client) -> None:
    repo = os.environ["AUTOSWE_FIXTURE_REPO"]
    created = api_client.post("/runs", json={"repo_url": repo, "goal": GOAL})
    created.raise_for_status()
    run_id = created.json()["run_id"]

    deadline = time.monotonic() + TIMEOUT_S
    summary: dict[str, object] = {}
    while time.monotonic() < deadline:
        summary = api_client.get(f"/runs/{run_id}").json()
        if summary["status"] in ("done", "failed"):
            break
        time.sleep(POLL_S)

    assert summary.get("status") == "done", f"run did not finish: {summary}"
    assert summary.get("pr_url"), "no pull request was opened"
    print(f"\nPR: {summary['pr_url']}  cost: ${summary['cost_usd']}")
