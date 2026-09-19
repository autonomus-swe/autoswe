"""Which image a repository gets, and how much of the machine.

The interesting half is the key. `RepoFacts.languages` is the obvious choice and is wrong
in three measured ways — a non-deterministic tie-break, two values that are not legal
Docker tags, and no entry for `.jsx`/`.mjs`/`.cjs` — so the tests below pin the decision to
`package_manager` and prove each of those three would have bitten.

A wrong answer here is not a wrong answer at the point of decision. It surfaces minutes
later as an opaque `SandboxError` from `containers.run` in the middle of SETUP.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from contracts import RepoFacts
from repo.profile import EXT_LANGUAGE
from sandbox import select

pytestmark = pytest.mark.unit


def facts(**kw: object) -> RepoFacts:
    return RepoFacts(**kw)


# ---- the image -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("manager", "image"),
    [
        ("uv", select.PYTHON_IMAGE),
        ("poetry", select.PYTHON_IMAGE),
        ("pip", select.PYTHON_IMAGE),
        ("npm", select.NODE_IMAGE),
        ("pnpm", select.NODE_IMAGE),
        ("yarn", select.NODE_IMAGE),
        ("go", select.GO_IMAGE),
    ],
)
def test_every_package_manager_the_detector_emits_has_an_image(manager: str, image: str) -> None:
    assert select.image_for(facts(package_manager=manager)) == image


def test_the_table_covers_everything_the_detector_can_return() -> None:
    """A manager the detector emits and this table does not know about is a repository
    silently downgraded to the Python image. Read from the detector rather than restated,
    so adding one there fails here rather than passing quietly."""
    import inspect

    from repo import profile

    source = inspect.getsource(profile.detect_package_manager)
    emitted = {
        line.split("return")[1].strip().strip('"')
        for line in source.splitlines()
        if "return" in line and '"' in line
    }

    assert emitted <= set(select.IMAGES), sorted(emitted - set(select.IMAGES))


def test_a_stack_nothing_recognises_still_gets_a_container() -> None:
    """A Rust or Ruby repository. The Python image carries git, ripgrep, bash and
    coreutils, so the agent can read, search and edit — it just cannot build. Refusing the
    run would turn 'we have no image for this' into 'you cannot use this tool here'."""
    assert select.image_for(facts(package_manager=None)) == select.PYTHON_IMAGE
    assert select.image_for(facts(package_manager="cargo")) == select.PYTHON_IMAGE


def test_no_facts_at_all_is_not_a_crash() -> None:
    """`RunState.facts` is optional, and a checkpoint written before detection ran has
    none."""
    assert select.image_for(None) == select.PYTHON_IMAGE


def test_a_deployment_can_pin_one_image_for_everything() -> None:
    """`SANDBOX_IMAGE` is the fallback, which means a deployment that built only the Python
    image keeps working exactly as it did."""
    assert select.image_for(facts(package_manager="rust"), default="my/own:image") == "my/own:image"


# ---- why not languages ---------------------------------------------------------------------


def test_the_language_list_holds_values_that_are_not_legal_docker_tags() -> None:
    """The first reason `image_for` is a table and not an f-string. `agent-sandbox:c++`
    fails at `containers.run` as an opaque SandboxError minutes into SETUP."""
    illegal = {lang for lang in EXT_LANGUAGE.values() if not lang.replace("-", "").isalnum()}

    assert illegal == {"c++", "c#"}, illegal
    assert not any(lang in select.IMAGES for lang in illegal), "and none of them is a key here"


def test_a_react_repository_reports_no_language_at_all() -> None:
    """The second reason. `EXT_LANGUAGE` has no `.jsx`, `.mjs` or `.cjs`, so a
    Create-React-App tree is invisible to a language-keyed decision — while its
    `package.json` is not."""
    assert ".jsx" not in EXT_LANGUAGE and ".mjs" not in EXT_LANGUAGE

    assert select.image_for(facts(package_manager="npm", languages=[])) == select.NODE_IMAGE


def test_the_decision_does_not_read_the_language_list_at_all() -> None:
    """The third reason is that `languages` ties break on filesystem order. Rather than
    test for non-determinism, pin that the decision never consults it: two repositories
    with opposite language rankings and the same manifest get the same image."""
    go_first = facts(package_manager="go", languages=["go", "python"])
    python_first = facts(package_manager="go", languages=["python", "go"])

    assert select.image_for(go_first) == select.image_for(python_first) == select.GO_IMAGE


def test_a_repository_with_both_a_pyproject_and_a_package_json_gets_python() -> None:
    """Not a judgement about which language it is mostly written in — `detect_package_manager`
    checks `pyproject.toml` first. Pinned because it is a precedence somebody will meet and
    should be able to find an answer for."""
    assert select.image_for(
        facts(package_manager="uv", manifests=["pyproject.toml", "package.json"])
    ) == (select.PYTHON_IMAGE)


# ---- limits ---------------------------------------------------------------------------------


def test_a_small_repository_gets_the_base_allowance() -> None:
    limits = select.limits_for(facts(package_manager="uv", file_count=50), select.PYTHON_IMAGE)

    assert limits.mem_limit == select.BASE_MEM and limits.cpus == select.BASE_CPUS


def test_a_large_repository_gets_more_memory() -> None:
    """The `/tmp` tmpfs is charged against the container's memory cgroup — measured:
    writing 300 MB into it moved `memory.current` by the same 300 MB — and the dependency
    cache lives there. A large repository is squeezed from both ends at once."""
    limits = select.limits_for(
        facts(package_manager="uv", file_count=select.LARGE_REPO_FILES + 1), select.PYTHON_IMAGE
    )

    assert limits.mem_limit == select.LARGE_MEM


def test_the_tmpfs_scales_with_the_memory_it_is_charged_against() -> None:
    """Raising the cgroup alone was half a fix: the cache lives on the tmpfs, and the
    tmpfs has its own hard `size=` ceiling, so a big repository would get `ENOSPC` at 1 GB
    however much memory the cgroup allowed."""
    small = select.limits_for(facts(file_count=10), select.PYTHON_IMAGE)
    large = select.limits_for(facts(file_count=select.LARGE_REPO_FILES + 1), select.PYTHON_IMAGE)

    assert small.tmpfs_size == select.BASE_TMPFS
    assert large.tmpfs_size == select.LARGE_TMPFS
    assert _gib(large.tmpfs_size) > _gib(small.tmpfs_size)


def test_the_tmpfs_never_exceeds_the_memory_it_is_charged_against() -> None:
    """It is a ceiling on one consumer inside the allowance, not the allowance."""
    for count in (10, select.LARGE_REPO_FILES + 1):
        limits = select.limits_for(facts(file_count=count), select.PYTHON_IMAGE)
        assert _gib(limits.tmpfs_size) < _gib(limits.mem_limit), count


def _gib(value: str) -> int:
    return int(value.removesuffix("g"))


def test_the_threshold_is_exclusive() -> None:
    """Exactly at the line is still a small repository, which is what `> 2000` says."""
    at = select.limits_for(
        facts(package_manager="uv", file_count=select.LARGE_REPO_FILES), select.PYTHON_IMAGE
    )

    assert at.mem_limit == select.BASE_MEM


@pytest.mark.parametrize("image", [select.NODE_IMAGE, select.GO_IMAGE])
def test_a_compiling_install_gets_more_cpu_and_only_for_the_install(image: str) -> None:
    """A Go build and a node-gyp build are the only CPU-bound part of a run. Everything
    after is waiting on a model, so the cores are handed back."""
    limits = select.limits_for(facts(package_manager="go", file_count=10), image)

    assert limits.install_cpus > limits.cpus
    assert limits.cpus == select.BASE_CPUS, "the steady-state allowance is unchanged"
    assert limits.raises_cpus_to_install


def test_a_python_install_does_not_take_extra_cpu() -> None:
    """`uv sync` unpacks wheels. There is nothing to compile, so there is nothing to buy."""
    limits = select.limits_for(facts(package_manager="uv", file_count=10), select.PYTHON_IMAGE)

    assert not limits.raises_cpus_to_install


def test_limits_follow_the_image_actually_used_not_the_facts() -> None:
    """A deployment that pinned `SANDBOX_IMAGE` to the Python image for a Node repository
    should get Python limits — otherwise it buys cores for a compile that will not happen."""
    node_repo = facts(package_manager="npm", file_count=10)

    assert not select.limits_for(node_repo, select.PYTHON_IMAGE).raises_cpus_to_install
    assert select.limits_for(node_repo, select.NODE_IMAGE).raises_cpus_to_install


def test_an_unknown_image_is_treated_as_not_compiling() -> None:
    """A pinned image nobody here has heard of. Assuming it compiles would hand cores to
    something that may not want them; assuming it does not costs only speed."""
    assert not select.limits_for(facts(file_count=10), "some/other:image").raises_cpus_to_install


# ---- an image this host never built ---------------------------------------------------------
#
# The regression this change introduced and an adversarial review caught. Choosing a better
# image is only an improvement if the host has it: `containers.run` on a missing tag does
# not fail, it tries to PULL, and these tags exist in no registry — so the run died in
# SETUP with "pull access denied ... may require 'docker login'", a message about
# authentication for a problem that is a missing local build.
#
# It passed every test on the machine it was written on, because that machine had all three.


def test_a_stack_whose_image_was_never_built_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The README tells you to run `make sandbox-image`, which builds only the Python one.
    Before this was a choice, a Node repository ran there and its install failed
    non-fatally. That has to stay true."""
    from core.settings import Settings
    from orchestrator import deps as deps_mod

    monkeypatch.setattr(deps_mod, "image_present", lambda tag, client=None: False)
    built = deps_mod.docker_sandbox_factory(
        Settings.model_construct(
            sandbox_image=select.PYTHON_IMAGE,
            sandbox_network="agent-install",
            sandbox_uid=1000,
            sandbox_gid=1000,
            sandbox_runtime=None,
            sandbox_no_new_privileges=True,
        )
    )

    sandbox = built(uuid4(), Path("/tmp"), facts(package_manager="npm"))

    assert sandbox.image == select.PYTHON_IMAGE


def test_a_stack_whose_image_exists_gets_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control. Without it the test above would pass on a factory that ignored the
    mapping entirely."""
    from core.settings import Settings
    from orchestrator import deps as deps_mod

    monkeypatch.setattr(deps_mod, "image_present", lambda tag, client=None: True)
    built = deps_mod.docker_sandbox_factory(
        Settings.model_construct(
            sandbox_image=select.PYTHON_IMAGE,
            sandbox_network="agent-install",
            sandbox_uid=1000,
            sandbox_gid=1000,
            sandbox_runtime=None,
            sandbox_no_new_privileges=True,
        )
    )

    sandbox = built(uuid4(), Path("/tmp"), facts(package_manager="npm"))

    assert sandbox.image == select.NODE_IMAGE


def test_the_fallback_also_takes_the_fallbacks_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Falling back to Python and still buying four cores for a node-gyp build that will
    never run would be paying for the capability we just lost."""
    from core.settings import Settings
    from orchestrator import deps as deps_mod

    monkeypatch.setattr(deps_mod, "image_present", lambda tag, client=None: False)
    built = deps_mod.docker_sandbox_factory(
        Settings.model_construct(
            sandbox_image=select.PYTHON_IMAGE,
            sandbox_network="agent-install",
            sandbox_uid=1000,
            sandbox_gid=1000,
            sandbox_runtime=None,
            sandbox_no_new_privileges=True,
        )
    )

    sandbox = built(uuid4(), Path("/tmp"), facts(package_manager="go", file_count=10))

    assert sandbox.cpus == select.BASE_CPUS  # type: ignore[attr-defined]
    assert not select.limits_for(None, sandbox.image).raises_cpus_to_install


def test_the_preflight_list_is_what_the_mapping_can_produce() -> None:
    """`scripts/bringup.sh` reads `ALL_IMAGES`. An image reachable from the mapping and
    absent from that list is one nothing ever reports as missing."""
    assert set(select.IMAGES.values()) <= set(select.ALL_IMAGES)
