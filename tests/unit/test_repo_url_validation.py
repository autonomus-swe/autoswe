"""`repo_url` is the only thing that decides which repository a run clones, pushes to and
opens a pull request on. It is checked once, in `RunCreate.only_github`, and never again.

One mutation of that check survived the whole suite:

| mutation | consequence |
|---|---|
| `parsed.scheme != "https"` → `False` | **a run clones and pushes over plain `http://`** |

It was invisible because the parametrised body test in `tests/integration/test_api.py`
covers a different host, a one-segment path, a short goal, an unknown field and a bad
branch — every clause of the rule except the scheme. `evals/suite.py` carries its own copy
of the rule and *is* tested for `http://`, which is the sort of coincidence that makes a
gap feel covered: the string appears in a test, in a different module, guarding a different
code path.

The consequence is not cosmetic. A clone over `http://` is unauthenticated and
unencrypted, and the push that follows carries a GitHub token in a header. Anything on the
path can read it, and the run itself cannot tell the difference — it succeeds.

Unit level, against the model directly: the rule is a field validator, and routing a
request through a server to reach it only adds ways for the test to fail for other
reasons.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.schemas import RunCreate

pytestmark = pytest.mark.unit

# Long enough to clear the 10-character floor on `goal`, so that a body which is refused is
# refused for its URL. `refusal_of` checks that too, rather than trusting this.
GOAL = "Implement subtract(a, b) and cover it with a unit test."


def refusal_of(repo_url: str) -> str:
    """The message the model refused this URL with, having checked it was the URL.

    Asserting only "a ValidationError was raised" would pass on a body rejected for its
    goal, its branch or a typo in this file — which is how a URL test stops testing URLs.
    """
    with pytest.raises(ValidationError) as caught:
        RunCreate(repo_url=repo_url, goal=GOAL)
    errors = caught.value.errors()
    locs = [e["loc"] for e in errors]
    assert locs == [("repo_url",)], f"{repo_url!r} was refused for {locs}, not for its URL"
    return str(errors[0]["msg"])


# ---- the scheme ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repo_url",
    [
        "http://github.com/acme/widget-service",
        "ftp://github.com/acme/widget-service",
        "git://github.com/acme/widget-service",
        "ssh://github.com/acme/widget-service",
    ],
)
def test_a_repository_url_that_is_not_https_is_refused(repo_url: str) -> None:
    """Every URL here satisfies the host rule and the owner/repo rule, so the scheme is the
    only clause that can refuse it. That is deliberate: a case two clauses both catch proves
    only that one of them works, and the scheme clause is the one that was not proven.

    `http://github.com/acme/widget-service` is the exact shape that got through: GitHub
    redirects it to https for a browser, and `git clone` follows the redirect, so a run
    would appear to work while the first request — and anything proxying it — travelled in
    clear text.
    """
    assert "https" in refusal_of(repo_url)


def test_an_https_github_url_is_accepted_unchanged() -> None:
    """The counterweight, and it is not a formality: `raise ValueError(...)` on every URL
    would satisfy all four refusal tests above and reject every run anyone tried to start.

    `www.github.com` is in the allowlist alongside `github.com`, so both must pass; and the
    value must come back as it went in, because what is stored here is what `git clone`
    is later handed.
    """
    for url in ("https://github.com/acme/widget-service", "https://www.github.com/acme/wid"):
        assert RunCreate(repo_url=url, goal=GOAL).repo_url == url


# ---- the host -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://gitlab.com/acme/widget-service",
        # Both of these contain the string "github.com" and neither is GitHub. A check
        # written as a substring test would wave them through, and the first is what a
        # phishing host looks like.
        "https://github.com.evil.example/acme/widget-service",
        "https://evil.example/github.com/acme/widget-service",
        "https://notgithub.com/acme/widget-service",
    ],
)
def test_a_repository_url_on_another_host_is_refused(repo_url: str) -> None:
    """A run is handed a GitHub token. Cloning from somewhere else is a run that sends
    that token wherever the caller named, and the only moment anyone can stop it is here.
    """
    assert "github.com" in refusal_of(repo_url)


# ---- owner and repository -----------------------------------------------------------------


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://github.com/acme",
        "https://github.com/acme/",
        "https://github.com/",
        "https://github.com",
    ],
)
def test_a_url_without_both_an_owner_and_a_repository_is_refused(repo_url: str) -> None:
    """`https://github.com/acme` is an account page, not a repository. Accepted here it
    becomes a clone that fails inside SETUP, minutes later, in a worker process the caller
    is not watching — instead of a 422 they can read and correct.
    """
    assert "owner" in refusal_of(repo_url)


def test_a_path_below_the_repository_is_still_a_repository() -> None:
    """The second counterweight. A rule written as "exactly two segments" would refuse the
    URL anyone gets by copying it out of their browser's address bar while looking at a
    file, which is where most of these come from.
    """
    url = "https://github.com/acme/widget-service/tree/release-2026"
    assert RunCreate(repo_url=url, goal=GOAL).repo_url == url


def test_surrounding_whitespace_is_removed_rather_than_carried_into_git() -> None:
    """A URL pasted out of a terminal arrives with a trailing newline. Stored with it, the
    newline reaches `git clone` as part of the argument and the failure names neither the
    newline nor the field it came from.
    """
    created = RunCreate(repo_url="  https://github.com/acme/widget-service\n", goal=GOAL)
    assert created.repo_url == "https://github.com/acme/widget-service"
