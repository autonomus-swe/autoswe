"""`${VAR}` expansion, and the empty value that is not a value.

`core.envsubst.expand` is the single place two configuration loaders decide whether a
credential exists: `mcp_servers.yaml` via `mcp_bridge/config.py`, and the eval task files
via `evals/suite.py`. It had no tests of its own. Both call sites exercised it from the
outside, and both did so with either `environ={}` or a plainly non-empty value.

That left the hole exactly where the common mistake is. This mutation survived the whole
suite:

    -    if not environ.get(name):
    +    if name not in environ:

Nothing in the suite distinguishes the two, because the only input that can is a variable
that is *present and empty*, and no test passed one. The function's own docstring promises
"An unset or empty variable raises"; the half about empty was unproven.

A `GITHUB_TOKEN=` line left in a `.env`, or a shell that flattened an unset variable into
the child environment, is a likelier accident than the variable being absent altogether.
What the mutation buys you is the thing this module exists to prevent: a GitHub server
that starts, answers `list_tools`, and fails on its first authenticated call somewhere
that names neither the file nor the variable; or an eval suite that reports every task
unresolved for a reason no result row records.

So the empty case is the centre of this file. The rest is here so that "refuse everything"
cannot pass for a fix: a real token has to survive, unmangled, in the right place in the
string, at both call sites.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.envsubst import expand
from core.errors import ConfigError
from evals import suite
from mcp_bridge import config

pytestmark = pytest.mark.unit

# A token nothing else in the suite produces, so an assertion on it cannot be satisfied by
# a default, a fixture leaking in, or the variable's own name coming back.
TOKEN = "ghp_7Qd4zdistinctive0token"

# The field named here deliberately shares no substring with the variable names below. The
# message has to carry both, and a `where` containing the variable name would let one
# assertion stand in for the other.
WHERE = "mcp_servers.yaml: server 'github': `env.FORGE_PAT`"


# ---- the gap: set but empty ---------------------------------------------------------------


def test_a_variable_that_is_set_but_empty_is_refused() -> None:
    """An empty credential otherwise reaches the server, and the failure lands nowhere near
    the file that caused it.

    This is the mutation that survived: `not environ.get(name)` → `name not in environ`.
    Both forms refuse a variable nobody set; only the first refuses `GITHUB_TOKEN=` with
    nothing after the equals sign, which is the accident operators actually have. Under the
    mutation the mounted server starts, answers `list_tools`, and fails on its first real
    call with a 401 that mentions neither this variable nor the file it was written in.
    """
    with pytest.raises(ConfigError) as caught:
        expand("${GITHUB_TOKEN}", {"GITHUB_TOKEN": ""}, where=WHERE)
    assert "GITHUB_TOKEN" in str(caught.value), (
        "the refusal must name the empty variable; an operator cannot act on 'a variable "
        f"is not set'. Got: {caught.value}"
    )


def test_a_variable_that_is_set_to_a_real_value_is_expanded() -> None:
    """The counterweight to every refusal in this file.

    `raise ConfigError` on sight of a `${` satisfies all of them, and leaves every mounted
    server without the token it was configured with. The value is also distinct from the
    variable's name, so returning the name instead of the value cannot pass here either.
    """
    assert expand("${GITHUB_TOKEN}", {"GITHUB_TOKEN": TOKEN}, where=WHERE) == TOKEN


def test_an_unset_variable_is_refused_even_when_other_variables_exist() -> None:
    """The other half of the promise, and the reason `if not environ:` is not the check.

    The environment handed to a loader is almost never empty, so a guard that asks whether
    *anything* is set would pass on every real process while letting the one variable the
    file names through as the empty string.
    """
    with pytest.raises(ConfigError) as caught:
        expand("${GITHUB_TOKEN}", {"SOME_OTHER_TOKEN": TOKEN}, where=WHERE)
    assert "GITHUB_TOKEN" in str(caught.value), (
        f"the refusal named the wrong variable, or none: {caught.value}"
    )


def test_the_refusal_names_the_file_and_field_as_well_as_the_variable() -> None:
    """The reader of this message is a person editing a configuration file.

    `where` is the only part that says which line to go and fix. Dropping it leaves
    "references ${GITHUB_TOKEN}, which is not set" against a repository with several files
    that could have said it, and the variable may legitimately be referenced by more than
    one of them.
    """
    with pytest.raises(ConfigError) as caught:
        expand("${GITHUB_TOKEN}", {"GITHUB_TOKEN": ""}, where=WHERE)
    message = str(caught.value)
    assert WHERE in message, f"the refusal lost the file and field it came from: {message}"
    assert "GITHUB_TOKEN" in message, f"the refusal lost the variable name: {message}"


# ---- substitution proper ------------------------------------------------------------------


def test_every_variable_in_the_string_is_substituted() -> None:
    """A connection string with only its first variable filled in is a connection to
    somewhere else, or to nowhere, and nothing in the error will mention this file.

    Asserted against the whole result rather than per variable: the literal text between
    the variables has to survive too, and a substitution that ate the separators would
    produce a string that still contained every value.
    """
    environ = {
        "DB_USER": "autoswe_rw",
        "DB_PASSWORD": "not-a-real-password",
        "DB_HOST": "db.internal.example:6543",
    }
    expanded = expand("postgres://${DB_USER}:${DB_PASSWORD}@${DB_HOST}/runs", environ, where=WHERE)
    assert expanded == "postgres://autoswe_rw:not-a-real-password@db.internal.example:6543/runs"


def test_a_variable_used_twice_is_substituted_in_both_places() -> None:
    """Repeating a variable is how one credential serves two settings in the same value.

    A first-match-only substitution leaves the second occurrence as the literal text
    `${GITHUB_TOKEN}`, which the server receives and sends as if it were the credential.
    """
    expanded = expand("${GITHUB_TOKEN}:${GITHUB_TOKEN}", {"GITHUB_TOKEN": TOKEN}, where=WHERE)
    assert expanded == f"{TOKEN}:{TOKEN}"


def test_an_empty_variable_is_refused_wherever_it_sits_in_the_string() -> None:
    """The check has to apply to every variable, not only the one that happens to be first.

    `postgres://user:@host/runs` is a perfectly well-formed string and an authentication
    attempt with no password. The earlier variable being set is what makes this case
    survivable for a loader that only looks once.
    """
    environ = {"DB_USER": "autoswe_rw", "DB_PASSWORD": "", "DB_HOST": "db.internal.example"}
    with pytest.raises(ConfigError) as caught:
        expand("postgres://${DB_USER}:${DB_PASSWORD}@${DB_HOST}/runs", environ, where=WHERE)
    assert "DB_PASSWORD" in str(caught.value), (
        f"the refusal must blame the empty variable, not merely refuse: {caught.value}"
    )


# ---- what is not a variable ---------------------------------------------------------------


def test_a_dollar_that_is_not_a_braced_name_is_left_exactly_as_written() -> None:
    """Shell-style `$HOME` is text here, and a password containing a dollar is a password.

    Only `${NAME}` is substitution. Widening that would rewrite `$HOME` inside a value
    against the *host's* environment, and would mangle any credential containing a `$` —
    both of which change a secret into something that merely looks like one.
    """
    literal = "p$$w0rd-$HOME-100$"
    assert expand(literal, {"HOME": "/root", "PATH": "/usr/bin"}, where=WHERE) == literal


def test_a_value_that_itself_contains_a_variable_is_not_expanded_again() -> None:
    """A secret whose text happens to look like a variable is returned as written.

    One pass, not a fixed point. A second pass would corrupt any token containing `${…}`
    and would refuse the configuration over a variable the operator never wrote — and a
    value that referred to itself would not terminate at all.
    """
    expanded = expand("${SECRET}", {"SECRET": "${NOT_A_VARIABLE_HERE}"}, where=WHERE)
    assert expanded == "${NOT_A_VARIABLE_HERE}", (
        "the substituted value was rescanned, so a secret's own text decided what happened to it"
    )


def test_a_value_containing_backslash_escapes_survives_intact() -> None:
    """A token containing `\\1` must arrive as a token, not as the regex engine's idea of it.

    `re.sub` interprets backslash escapes in a *string* replacement but not in the return
    value of a function. Swap this implementation for a template-style one and `\\1` becomes
    the captured variable name and `\\g<0>` the whole `${…}` match, so the credential is
    silently rewritten and authentication fails against a value nobody typed.
    """
    awkward = r"ghp_\1backslash\g<0>end"
    assert expand("${GITHUB_TOKEN}", {"GITHUB_TOKEN": awkward}, where=WHERE) == awkward


def test_a_string_with_no_variables_is_returned_unchanged() -> None:
    """Most configuration values contain no variables at all, and they are the ones nobody
    would notice being mangled until a server was started with the wrong argument."""
    assert expand("npx -y @modelcontextprotocol/server-github", {}, where=WHERE) == (
        "npx -y @modelcontextprotocol/server-github"
    )
    assert expand("", {}, where=WHERE) == ""


# ---- the two call sites that share this check ---------------------------------------------
#
# The point of the module is that there is one check rather than a copy in each loader, so
# each loader is asked the question the shared check exists to answer.


def test_an_empty_token_in_mcp_servers_yaml_stops_the_load(tmp_path: Path) -> None:
    """docs/security.md §7 claims an unset `${VAR}` in a config file is an error rather
    than an empty credential. An empty one has to be the same error, or a mounted GitHub
    server is handed `GITHUB_PERSONAL_ACCESS_TOKEN=""` and comes up looking healthy.

    The second half is the counterweight: refusing every file would satisfy the first and
    mount nothing at all.
    """
    path = tmp_path / "mcp_servers.yaml"
    path.write_text(
        "servers:\n"
        "  github:\n"
        "    transport: stdio\n"
        '    command: ["npx", "-y", "@modelcontextprotocol/server-github"]\n'
        "    env:\n"
        '      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"\n'
        "    roles: [coder]\n"
        "    allow: [get_issue]\n"
    )

    with pytest.raises(ConfigError) as caught:
        config.load(path, environ={"GITHUB_TOKEN": ""})
    assert "GITHUB_TOKEN" in str(caught.value), str(caught.value)

    [github] = config.load(path, environ={"GITHUB_TOKEN": TOKEN})
    assert github.env == {"GITHUB_PERSONAL_ACCESS_TOKEN": TOKEN}, (
        "a real token must reach the server's environment; the refusal above is only "
        "worth anything if this still works"
    )


def test_an_empty_fixture_repo_stops_an_eval_suite_by_name(tmp_path: Path) -> None:
    """A suite loaded with `AUTOSWE_FIXTURE_REPO=` would otherwise cost an hour to learn
    nothing: every task pointed at a repository that is the empty string.

    The assertion is on the variable's *name* on purpose. `_repo` rejects the empty string
    too, with "must be an https://github.com/owner/name URL" — so a test that only checked
    for a `ConfigError` would pass with `expand` gutted, and would be proving the URL
    validator rather than this check. Only `expand` can name the variable.
    """
    tasks = tmp_path / "tasks" / "fixtures"
    tasks.mkdir(parents=True)
    (tasks / "one.yaml").write_text(
        "id: empty-credential\n"
        'goal: "Add a regression test for the empty credential path and make it pass."\n'
        'repo: "${AUTOSWE_FIXTURE_REPO}"\n'
    )

    with pytest.raises(ConfigError) as caught:
        suite.load("fixtures", directory=tmp_path / "tasks", environ={"AUTOSWE_FIXTURE_REPO": ""})
    assert "AUTOSWE_FIXTURE_REPO" in str(caught.value), (
        "an empty repository was refused by the URL validator rather than by the variable "
        f"check, so the operator is told nothing about what to set: {caught.value}"
    )

    loaded = suite.load(
        "fixtures",
        directory=tmp_path / "tasks",
        environ={"AUTOSWE_FIXTURE_REPO": "https://github.com/acme/distinctive-fixture"},
    )
    assert loaded.tasks[0].repo == "https://github.com/acme/distinctive-fixture", (
        "a suite with its fixture repository set must still load"
    )
