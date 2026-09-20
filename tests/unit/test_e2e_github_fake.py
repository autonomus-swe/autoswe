"""The end-to-end GitHub stub has to model the part `open_pr` actually uses.

`open_pr` applies labels *after* creating the pull request, because `create_pull` takes
none and a label that does not exist in the repository is a 422 on a pull request that is
already open. Losing the pull request over a missing label would be the wrong trade, so
that call is best-effort: it logs and carries on.

Which is precisely why a stub that omits `add_to_labels` is worse than it looks. The
`AttributeError` is swallowed, so nothing fails — a completed end-to-end run simply emits
`pr_labels_failed` and looks like it has a product defect. Measured: a real run did exactly
that, and the warning names the stub's own class.

The second cost is the one that matters for the suite. With the error swallowed, "labels
were applied" and "labelling failed" are indistinguishable from outside, so no end-to-end
test could ever catch a regression in that path. The integration stub records labels for
this reason and says so; this one did not.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import RecordingGitHub, RecordingPR

pytestmark = pytest.mark.unit


def test_the_pull_request_accepts_the_call_open_pr_makes_on_it() -> None:
    """The missing method, and the whole bug: `open_pr` calls exactly this."""
    pr = RecordingPR("local://pull/1")

    pr.add_to_labels("autoswe", "needs-review")

    assert pr.labels == ["autoswe", "needs-review"]


def test_labels_are_visible_so_applied_can_be_told_from_swallowed() -> None:
    """`open_pr` treats a labelling failure as best-effort. Without a record, a test cannot
    distinguish the two outcomes — which is the same as not testing the path."""
    github = RecordingGitHub()

    pr = github.create_pull(title="t", body="b", head="h", base="main")
    pr.add_to_labels("autoswe")

    assert github.labelled == ["autoswe"]


def test_a_stub_with_no_pull_requests_has_no_labels() -> None:
    """The control. `labelled` reading empty has to mean "nothing was labelled" rather
    than "the accessor is broken", or the assertion above proves nothing."""
    assert RecordingGitHub().labelled == []


def test_each_pull_request_keeps_its_own_labels() -> None:
    """A run can open more than one. Pooling them onto the stub would make a test that
    checks the second pull request pass on the first one's labels."""
    github = RecordingGitHub()

    first = github.create_pull(title="a", body="", head="h1", base="main")
    second = github.create_pull(title="b", body="", head="h2", base="main")
    first.add_to_labels("one")
    second.add_to_labels("two")

    assert first.labels == ["one"] and second.labels == ["two"]
    assert github.labelled == ["one", "two"]


def test_every_pull_request_still_carries_a_url() -> None:
    """`open_pr` returns `html_url` and the PR node stores it as the run's artifact, so a
    stub that lost it would break the phase rather than a label."""
    github = RecordingGitHub()

    urls = [
        github.create_pull(title=str(i), body="", head="h", base="main").html_url for i in range(2)
    ]

    assert all(u.startswith("local://pull/") for u in urls)
    assert len(set(urls)) == 2, "two pull requests, two urls"
