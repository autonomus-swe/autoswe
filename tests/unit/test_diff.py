"""The run's diff — the Reviewer's entire view of what changed.

Built against diffs a real git produced, not hand-written approximations. The parser's job
is to survive what git actually emits, and the two times I have been bitten in this project
were both a sample that looked right and was not: pytest's json report carrying no
traceback for a collection failure, and `crash` duplicating a traceback entry with an
absolute path. So these tests make commits in a temporary repository and read what comes
back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repo import diff as d
from repo.gitcmd import git

pytestmark = pytest.mark.unit


async def repo_with(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    """A git repository with one commit, returning ``(path, base_sha)``."""
    root = tmp_path / "repo"
    root.mkdir()
    await git("init", "-q", "-b", "main", cwd=root)
    for name, body in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    await git("add", "-A", cwd=root)
    await git("commit", "-q", "-m", "base", cwd=root)
    sha = (await git("rev-parse", "HEAD", cwd=root)).strip()
    return root, sha


async def commit(root: Path, files: dict[str, str], message: str = "change") -> None:
    for name, body in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    await git("add", "-A", cwd=root)
    await git("commit", "-q", "-m", message, cwd=root)


# ---- what git actually emits ---------------------------------------------------------


async def test_a_modified_file_reports_its_new_side_line_numbers(tmp_path: Path) -> None:
    """A finding has to point at a line a reader can open, so the `+` side is what counts."""
    root, sha = await repo_with(tmp_path, {"src/a.py": "one\ntwo\nthree\nfour\nfive\n"})
    await commit(root, {"src/a.py": "one\ntwo\nCHANGED\nfour\nfive\nSIX\n"})

    files = d.split_by_file(await d.full_diff(root, sha))

    (f,) = files
    assert f.path == "src/a.py"
    assert (f.added, f.removed) == (2, 1)
    # line 3 was rewritten, line 6 appended — both on the new side
    assert f.added_lines == {3, 6}
    assert d.touches(files, "src/a.py", 3) and d.touches(files, "src/a.py", 6)
    assert not d.touches(files, "src/a.py", 1), "an untouched line is not part of the change"


async def test_an_untracked_file_is_part_of_the_change(tmp_path: Path) -> None:
    """A run that writes a file and never commits it has still changed the repository."""
    root, sha = await repo_with(tmp_path, {"src/a.py": "one\n"})
    (root / "src" / "new.py").write_text("def added():\n    return 1\n")

    files = d.split_by_file(await d.full_diff(root, sha))

    assert [f.path for f in files] == ["src/new.py"]
    assert files[0].added == 2 and files[0].added_lines == {1, 2}


async def test_collecting_the_diff_leaves_the_real_index_alone(tmp_path: Path) -> None:
    """Reviewing must not disturb what the agent had staged."""
    root, sha = await repo_with(tmp_path, {"a.py": "one\n", "b.py": "one\n"})
    (root / "a.py").write_text("two\n")
    await git("add", "a.py", cwd=root)  # staged on purpose
    (root / "c.py").write_text("new\n")  # untracked on purpose
    before = await git("status", "--porcelain", cwd=root)

    await d.full_diff(root, sha)

    assert await git("status", "--porcelain", cwd=root) == before
    assert not (root / ".git" / "autoswe-review-index").exists(), "the temporary index is removed"


async def test_a_deleted_file_counts_as_removal(tmp_path: Path) -> None:
    root, sha = await repo_with(tmp_path, {"gone.py": "one\ntwo\n", "stay.py": "x\n"})
    (root / "gone.py").unlink()
    await commit(root, {}, "delete")

    files = d.split_by_file(await d.full_diff(root, sha))

    (f,) = files
    assert f.path == "gone.py" and f.removed == 2 and f.added == 0
    assert f.added_lines == set(), "nothing on the new side to point at"


async def test_several_files_keep_gits_order_and_separate_cleanly(tmp_path: Path) -> None:
    root, sha = await repo_with(tmp_path, {"a.py": "1\n", "b.py": "1\n", "c.py": "1\n"})
    await commit(root, {"a.py": "1\n2\n", "b.py": "1\n2\n", "c.py": "1\n2\n"})

    files = d.split_by_file(await d.full_diff(root, sha))

    assert [f.path for f in files] == ["a.py", "b.py", "c.py"]
    assert all(f.added == 1 and f.added_lines == {2} for f in files)
    # each file's text is its own: no header bleeding into the next
    assert all(f.text.count("diff --git") == 1 for f in files)


async def test_no_change_is_no_files(tmp_path: Path) -> None:
    root, sha = await repo_with(tmp_path, {"a.py": "one\n"})
    assert d.split_by_file(await d.full_diff(root, sha)) == []
    assert d.diff_stat([]) == "no changes"


async def test_it_works_in_a_linked_worktree_where_dot_git_is_a_file(tmp_path: Path) -> None:
    """The shape every run actually uses, and the one the other tests here do not exercise.

    `git init` gives you a `.git` directory; a linked worktree gives you a `.git` *file*
    holding a pointer. Putting the temporary index at `<worktree>/.git/...` therefore works
    in every test above and fails in production with `Not a directory` — which is what
    happened, caught by an integration test rather than by these.
    """
    bare = tmp_path / "bare.git"
    src = tmp_path / "src"
    src.mkdir()
    await git("init", "-q", "-b", "main", cwd=src)
    (src / "a.py").write_text("one\n")
    await git("add", "-A", cwd=src)
    await git("commit", "-q", "-m", "base", cwd=src)
    await git("clone", "-q", "--bare", str(src), str(bare))

    wt = tmp_path / "wt"
    await git("worktree", "add", "-q", str(wt), "main", cwd=bare)
    sha = (await git("rev-parse", "HEAD", cwd=wt)).strip()
    assert (wt / ".git").is_file(), "the premise: .git is a file here, not a directory"

    (wt / "a.py").write_text("two\n")
    (wt / "new.py").write_text("added\n")

    files = d.split_by_file(await d.full_diff(wt, sha))

    assert sorted(f.path for f in files) == ["a.py", "new.py"]
    gitdir = Path((await git("rev-parse", "--absolute-git-dir", cwd=wt)).strip())
    assert not (gitdir / "autoswe-review-index").exists(), "the temporary index is cleaned up"


# ---- the budget: what the model is allowed to see -------------------------------------


async def test_a_lock_file_is_summarised_and_the_code_is_not(tmp_path: Path) -> None:
    """A 40,000-line lock file crowds out the twelve lines that matter."""
    root, sha = await repo_with(tmp_path, {"uv.lock": "v = 1\n", "src/a.py": "one\n"})
    await commit(
        root,
        {"uv.lock": "\n".join(f'pkg-{i} = "1.0"' for i in range(4000)), "src/a.py": "one\ntwo\n"},
    )

    files = d.split_by_file(await d.full_diff(root, sha))
    lock = next(f for f in files if f.path == "uv.lock")
    code = next(f for f in files if f.path == "src/a.py")

    assert lock.generated and d.too_large(lock)
    assert not code.generated and not d.too_large(code)

    body = d.for_model(files)
    assert "uv.lock: +4000 -1 (generated; full text in the diff artifact)" in body
    assert 'pkg-1 = "1.0"' not in body, "the model must not be handed the lock file's text"
    assert "+two" in body, "and must still be handed the code"
    # the artifact keeps everything, because "what actually changed" deserves an answer
    assert 'pkg-1 = "1.0"' in lock.text


@pytest.mark.parametrize(
    "path",
    ["uv.lock", "package-lock.json", "yarn.lock", "Cargo.lock", "go.sum", "web/app.min.js"],
)
def test_the_generated_list_covers_what_ecosystems_actually_commit(path: str) -> None:
    assert d.is_generated(path)


@pytest.mark.parametrize("path", ["src/lock.py", "tests/test_package_lock.py", "app.js"])
def test_a_hand_written_file_is_not_generated_because_of_its_name(path: str) -> None:
    assert not d.is_generated(path)


def test_a_long_migration_is_summarised_sooner_than_ordinary_code() -> None:
    """Migrations are hand-written but mechanical, so the cut-off is lower."""
    body = "\n".join(f"+line {i}" for i in range(d.MAX_MIGRATION_DIFF_LINES + 10))
    migration = d.FileDiff("storage/migrations/versions/0003_x.py", 400, 0, body)
    ordinary = d.FileDiff("src/big.py", 400, 0, body)

    assert d.too_large(migration)
    assert not d.too_large(ordinary), "the same length is fine in hand-written code"


async def test_the_stat_reads_like_gits_own(tmp_path: Path) -> None:
    root, sha = await repo_with(tmp_path, {"a.py": "1\n", "b.py": "1\n"})
    await commit(root, {"a.py": "1\n2\n3\n", "b.py": ""})

    stat = d.diff_stat(d.split_by_file(await d.full_diff(root, sha)))

    assert "a.py | +2 -0" in stat
    assert "2 files changed, 2 insertions(+), 1 deletions(-)" in stat


def test_one_file_is_not_called_files() -> None:
    assert "1 file changed" in d.diff_stat([d.FileDiff("a.py", 1, 0, "+x")])


# ---- the question the scanners ask ----------------------------------------------------


async def test_a_finding_outside_the_change_is_not_in_the_diff(tmp_path: Path) -> None:
    """Only new findings can block a run: a vulnerability the agent did not introduce is
    worth reporting and is not its to answer for."""
    root, sha = await repo_with(
        tmp_path, {"src/a.py": "safe\n" * 10 + "old_problem\n" + "safe\n" * 10}
    )
    await commit(root, {"src/a.py": "safe\n" * 10 + "old_problem\n" + "safe\n" * 10 + "new_line\n"})

    files = d.split_by_file(await d.full_diff(root, sha))

    assert d.touches(files, "src/a.py", 22), "the line the run added"
    assert not d.touches(files, "src/a.py", 11), "the pre-existing problem"
    assert not d.touches(files, "other.py", 1), "a file the run never opened"


def test_asking_about_an_unknown_file_is_false_not_an_error() -> None:
    assert d.touches([], "anything.py", 1) is False
