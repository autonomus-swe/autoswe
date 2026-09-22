"""Opening a pull request somewhere other than where the branch was pushed.

The fork workflow this project insists on for its own development is the one it could not
offer its users: a run cloned `repo_url`, pushed a branch there, and opened the pull
request there. That works when you own the repository and not otherwise — and "not
otherwise" is the case the pattern exists for.

The subtle half is the `head` used to *search* for an existing pull request, not the one
used to create it. Both have to be owner-qualified, and the test that says so is the one
about reuse.
"""

from __future__ import annotations

from typing import Any

import pytest

from repo.github import open_pr

pytestmark = pytest.mark.unit


class FakePull:
    def __init__(self, html_url: str, head: str) -> None:
        self.html_url = html_url
        self.head = head
        self.labels: list[str] = []

    def add_to_labels(self, *names: str) -> None:
        self.labels.extend(names)


class FakeRepo:
    def __init__(self, full_name: str, existing: list[FakePull] | None = None) -> None:
        self.full_name = full_name
        self.existing = existing or []
        self.searched: list[str] = []
        self.created: list[dict[str, Any]] = []

    def get_pulls(self, state: str, head: str) -> list[FakePull]:
        self.searched.append(head)
        return [p for p in self.existing if p.head == head]

    def create_pull(self, **kwargs: Any) -> FakePull:
        self.created.append(kwargs)
        return FakePull(f"https://github.com/{self.full_name}/pull/1", str(kwargs["head"]))


class FakeGitHub:
    def __init__(self, repos: dict[str, FakeRepo]) -> None:
        self.repos = repos
        self.asked: list[str] = []

    def get_repo(self, full_name: str) -> FakeRepo:
        self.asked.append(full_name)
        return self.repos[full_name]


async def test_without_an_upstream_nothing_changes() -> None:
    """The ordinary case is the overwhelming majority of runs, and it must keep its exact
    previous behaviour — an unqualified `head` against the repository we pushed to."""
    repo = FakeRepo("me/project")
    client = FakeGitHub({"me/project": repo})

    url = await open_pr(
        "https://github.com/me/project",
        head="agent/abc",
        base="main",
        title="t",
        body="b",
        client=client,
    )
    assert client.asked == ["me/project"]
    assert repo.created[0]["head"] == "agent/abc"
    assert url.endswith("/pull/1")


async def test_with_an_upstream_the_pr_is_opened_there_with_a_qualified_head() -> None:
    """The branch is on the fork; GitHub reads the owner prefix as "it is over there"."""
    upstream = FakeRepo("them/project")
    client = FakeGitHub({"them/project": upstream, "me/project": FakeRepo("me/project")})

    url = await open_pr(
        "https://github.com/me/project",
        head="agent/abc",
        base="main",
        title="t",
        body="b",
        client=client,
        upstream="them/project",
    )
    assert client.asked == ["them/project"]  # never the fork
    assert upstream.created[0]["head"] == "me:agent/abc"
    assert upstream.created[0]["base"] == "main"
    assert "them/project" in url


async def test_the_search_for_an_existing_pr_is_qualified_too() -> None:
    """The half that matters and is easy to get wrong.

    `get_pulls(head="agent/abc")` on somebody else's repository matches a branch of that
    name **on their repository** — a different branch belonging to a different person.
    Reusing it would mean pushing our commits' description onto a stranger's pull request
    and reporting its URL as ours.
    """
    stranger = FakePull("https://github.com/them/project/pull/99", "agent/abc")
    ours = FakePull("https://github.com/them/project/pull/100", "me:agent/abc")
    upstream = FakeRepo("them/project", existing=[stranger, ours])
    client = FakeGitHub({"them/project": upstream})

    url = await open_pr(
        "https://github.com/me/project",
        head="agent/abc",
        base="main",
        title="t",
        body="b",
        client=client,
        upstream="them/project",
    )
    assert upstream.searched == ["me:agent/abc"]
    assert url.endswith("/pull/100")  # ours, not the stranger's
    assert upstream.created == []  # reused rather than opened again


async def test_labels_are_applied_on_the_upstream_pull_request() -> None:
    upstream = FakeRepo("them/project")
    client = FakeGitHub({"them/project": upstream})
    await open_pr(
        "https://github.com/me/project",
        head="agent/abc",
        base="main",
        title="t",
        body="b",
        client=client,
        upstream="them/project",
        labels=["autoswe"],
    )
    assert upstream.created  # created rather than reused, and labelling did not raise


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("them/project", True),
        ("them/project/extra", False),
        ("them", False),
        ("https://github.com/them/project", False),
        ("", False),
    ],
)
def test_the_api_takes_owner_repo_and_not_a_url(value: str, ok: bool) -> None:
    """GitHub's API takes the pair. A URL here would be accepted and then fail at PR time,
    after the branch had already been pushed — the most expensive moment to find out."""
    from pydantic import ValidationError

    from api.schemas import RunCreate

    body = {
        "repo_url": "https://github.com/me/project",
        "goal": "Implement subtract(a, b).",
        "upstream": value,
    }
    if ok:
        assert RunCreate(**body).upstream == value
    else:
        with pytest.raises(ValidationError):
            RunCreate(**body)


def test_no_upstream_is_the_default() -> None:
    from api.schemas import RunCreate

    body = RunCreate(repo_url="https://github.com/me/project", goal="Implement subtract(a, b).")
    assert body.upstream is None
