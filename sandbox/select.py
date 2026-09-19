"""Which image a repository gets, and how much of the machine it is allowed.

## Keyed on the package manager, not on the language

`RepoFacts.languages` is the obvious key and the wrong one, for three measured reasons:

- **Its order is not deterministic.** `_languages` sorts by descending count with Python's
  stable sort, so an exact tie falls back to `rglob` traversal order — which is filesystem
  order, not sorted. The same repository can rank differently after a reclone, and picking
  `languages[0]` would then pick a different image for the same commit.
- **Two of its values are not legal Docker tags.** `EXT_LANGUAGE` emits `c++` and `c#`, so
  anything that built a tag from the language string would produce `agent-sandbox:c++` and
  fail at `containers.run` as an opaque error in the middle of SETUP.
- **Its vocabulary has holes exactly where a second image would help.** There is no `.jsx`,
  `.mjs` or `.cjs`, so a React repository of `.jsx` files reports no language at all.

`package_manager` has none of those problems. It is decided by which manifest files exist —
a handful of `is_file()` checks in a fixed order — so it is single-valued, deterministic,
and already distinguishes the ecosystems the images are for.

**Its precedence is worth knowing:** `detect_package_manager` checks `pyproject.toml`
first, so a repository holding both a `pyproject.toml` and a `package.json` reports `uv`
and gets the Python image. That is a real answer for a mixed repository rather than a
lucky one — the Python toolchain is the one the harness itself needs — but it is a
precedence, not a judgement about which language the repository is mostly written in.

## An unknown stack gets the Python image

Not an error. The Python image carries `git`, `ripgrep`, `bash` and `coreutils`, so an
agent can still read, search and edit in a Rust or Ruby repository — it simply cannot build
one. Refusing the run instead would turn "we have no image for this" into "you cannot use
this tool here", which is worse and less true.

## What a non-Python image costs

Honest about the gap: `bandit`, `semgrep` and its vendored rules are baked into the Python
image and nowhere else, and `tools/scanners.py` runs them inside whatever container the run
got. A repository routed to the Node or Go image therefore gets no in-sandbox static
analysis.

That does not fail silently — `scanners.failure()` turns a scanner that could not run into
an `info` finding saying so, and it reaches the pull request body — but it is a real
capability gap rather than a rough edge. `gitleaks` and `pip-audit` still run on the
worker, so secrets in commits are still scanned; secrets in *untracked* files, which only
semgrep's `generic/secrets/security` pack catches, are not.

The fix is per-image scanners, which is a larger piece of work than choosing an image.

## Limits

Two dials, and they move for different reasons.

**Memory** scales with the repository, and so does the `/tmp` tmpfs, because they are the
same budget: the tmpfs is charged against the container's memory cgroup — measured, writing
300 MB into `/tmp` moved `memory.current` by the same 300 MB.

Raising only the cgroup would have been half a fix and a misleading comment. The dependency
cache lives on that tmpfs, and the tmpfs has its own hard `size=` ceiling; a repository that
needs a bigger cache gets `ENOSPC` at 1 GB however much memory the cgroup allows. So both
move together, and the tmpfs stays the smaller of the two — it is a ceiling on one consumer,
not the whole allowance.

**CPU** is raised only while dependencies install, and that is possible because the phase
document's premise is wrong. It says a container cannot be changed after creation, so the
higher value must be predicted up front. Measured on this host: `container.update()` moves
both `memory.max` and `cpu.max` on a running container with no restart. So the install gets
the CPUs and the agent's own commands do not — which matters, because a Go or Node install
is a compile and the rest of a run is mostly waiting on a model.
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts import RepoFacts
from observability.logging import get_logger

log = get_logger(__name__)

PYTHON_IMAGE = "agent-sandbox:python-3.12"
NODE_IMAGE = "agent-sandbox:node-20"
GO_IMAGE = "agent-sandbox:go-1.23"

# Package manager -> image. Explicit rather than derived from the string, so a manager this
# table does not know about falls to the default instead of naming an image nobody built.
IMAGES: dict[str, str] = {
    "uv": PYTHON_IMAGE,
    "poetry": PYTHON_IMAGE,
    "pip": PYTHON_IMAGE,
    "npm": NODE_IMAGE,
    "pnpm": NODE_IMAGE,
    "yarn": NODE_IMAGE,
    "go": GO_IMAGE,
}

# Every image this build can select. `scripts/bringup.sh` reads this list rather than
# restating it, so an image added here is reported as missing without anyone remembering
# to update the shell.
ALL_IMAGES: tuple[str, ...] = (PYTHON_IMAGE, NODE_IMAGE, GO_IMAGE)

BASE_MEM = "4g"
LARGE_MEM = "6g"
# The tmpfs the dependency cache and HOME live on. Smaller than the cgroup allowance
# because it is a ceiling on one consumer inside it, not the whole budget.
BASE_TMPFS = "1g"
LARGE_TMPFS = "2g"
# Past this many files a repository's dependency tree and its tmpfs cache stop fitting
# comfortably beside each other in the base allowance.
LARGE_REPO_FILES = 2_000
BASE_CPUS = 2.0
# Installs that compile. A Go build and a node-gyp build are the only parts of a run that
# are CPU-bound; everything after is waiting on a model.
COMPILING_INSTALL_CPUS = 4.0
COMPILES_ON_INSTALL = frozenset({NODE_IMAGE, GO_IMAGE})


@dataclass(frozen=True)
class Limits:
    """What one container may use. `install_cpus` applies only while dependencies install."""

    mem_limit: str
    tmpfs_size: str
    cpus: float
    install_cpus: float

    @property
    def raises_cpus_to_install(self) -> bool:
        return self.install_cpus > self.cpus


def image_for(facts: RepoFacts | None, default: str = PYTHON_IMAGE) -> str:
    """The image for this repository, or `default` when nothing recognises it.

    Names the image this stack *wants*. Whether the host has built it is a separate
    question, asked by `docker_sandbox_factory`, which falls back to the default when the
    answer is no — so a deployment that built only the Python image behaves exactly as it
    did before any of this was a choice.

    `facts` is optional because `RunState.facts` is, even though every caller reaches this
    with facts in hand — a signature that cannot express "not detected yet" invites a
    caller to fabricate one.
    """
    manager = (facts.package_manager if facts else None) or ""
    image = IMAGES.get(manager)
    if image is None:
        log.info(
            "sandbox_image_default",
            package_manager=manager or "none detected",
            image=default,
            note="no image for this stack; the agent can read and edit but not build",
        )
        return default
    return image


def limits_for(facts: RepoFacts | None, image: str) -> Limits:
    """How much of the machine this run gets.

    Takes the resolved image rather than re-deriving it, so a caller that overrode the
    image — a deployment pinning one, a test — gets limits that match what it is running
    rather than what the facts imply.
    """
    large = bool(facts and facts.file_count > LARGE_REPO_FILES)
    install_cpus = COMPILING_INSTALL_CPUS if image in COMPILES_ON_INSTALL else BASE_CPUS
    return Limits(
        mem_limit=LARGE_MEM if large else BASE_MEM,
        tmpfs_size=LARGE_TMPFS if large else BASE_TMPFS,
        cpus=BASE_CPUS,
        install_cpus=install_cpus,
    )
