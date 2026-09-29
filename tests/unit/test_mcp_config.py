"""`mcp_servers.yaml`, and the configurations it refuses.

Almost every test here is a rejection, which is the right shape for this file. The
configuration decides which external tools a model may call, which agents get them, and
which of them need a human's approval — so the interesting cases are the ones where a
plausible-looking file would have quietly disarmed one of those.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.errors import ConfigError
from mcp_bridge import config

pytestmark = pytest.mark.unit

VALID = """
servers:
  github:
    transport: stdio
    command: ["npx", "-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"
    roles: [coder, debugger]
    allow: [get_issue, add_issue_comment]
    mutating: [add_issue_comment]
"""


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "mcp_servers.yaml"
    path.write_text(body)
    return path


def test_a_missing_file_means_no_servers(tmp_path: Path) -> None:
    """Mounting is opt-in. A deployment that wants none should not have to say so."""
    assert config.load(tmp_path / "absent.yaml") == []


def test_a_valid_file_parses(tmp_path: Path) -> None:
    [github] = config.load(write(tmp_path, VALID), environ={"GITHUB_TOKEN": "ghp_xyz"})
    assert github.name == "github"
    assert github.command[0] == "npx"
    assert github.roles == ["coder", "debugger"]
    assert github.allow == ["get_issue", "add_issue_comment"]
    assert github.mutating == ["add_issue_comment"]
    assert github.env == {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_xyz"}


def test_tool_names_are_prefixed_with_the_server(tmp_path: Path) -> None:
    """A server exposing `read_file` would otherwise shadow ours, and a model calling it
    would be reading the wrong machine's disk with nothing to say so."""
    [github] = config.load(write(tmp_path, VALID), environ={"GITHUB_TOKEN": "t"})
    assert github.tool_name("read_file") == "mcp_github_read_file"


def test_an_unset_variable_is_an_error_not_an_empty_string(tmp_path: Path) -> None:
    """A server started with an empty token comes up, answers `list_tools`, and fails on
    its first real call — somewhere that mentions neither this file nor the variable."""
    with pytest.raises(ConfigError, match=r"GITHUB_TOKEN"):
        config.load(write(tmp_path, VALID), environ={})


def test_a_mutating_tool_may_not_go_to_a_read_only_role(tmp_path: Path) -> None:
    """The Phase 6 plan's own example sets `roles: [planner, analyzer, pr_writer]` beside
    `mutating: [add_issue_comment]`. All three are read-only roles, so that configuration
    is rejected — by the rule the same plan asks for two paragraphs later."""
    body = VALID.replace("roles: [coder, debugger]", "roles: [planner, analyzer, pr_writer]")
    with pytest.raises(ConfigError, match=r"read-only roles"):
        config.load(write(tmp_path, body), environ={"GITHUB_TOKEN": "t"})


def test_a_read_only_role_may_have_read_only_tools(tmp_path: Path) -> None:
    """The rule is about mutating tools, not about mounting at all — an Analyzer querying
    a read-only Postgres server is the case the criterion asks for."""
    body = """
servers:
  postgres:
    command: ["npx", "-y", "@modelcontextprotocol/server-postgres"]
    roles: [analyzer]
    allow: [query]
    read_only: true
"""
    [postgres] = config.load(write(tmp_path, body), environ={})
    assert postgres.roles == ["analyzer"] and postgres.mutating == []


def test_a_mutating_name_must_also_be_allowed(tmp_path: Path) -> None:
    """A typo in `mutating` names nothing, so the tool it was meant to guard is mounted
    without approval — a misspelling that silently disarms the gate."""
    body = VALID.replace("mutating: [add_issue_comment]", "mutating: [add_issue_commnet]")
    with pytest.raises(ConfigError, match=r"listed as mutating but not allowed"):
        config.load(write(tmp_path, body), environ={"GITHUB_TOKEN": "t"})


def test_read_only_and_mutating_together_are_a_contradiction(tmp_path: Path) -> None:
    body = VALID.replace("    mutating:", "    read_only: true\n    mutating:")
    with pytest.raises(ConfigError, match=r"read_only is set"):
        config.load(write(tmp_path, body), environ={"GITHUB_TOKEN": "t"})


def test_an_empty_allow_list_is_a_typo_not_an_intention(tmp_path: Path) -> None:
    body = VALID.replace("allow: [get_issue, add_issue_comment]", "allow: []").replace(
        "mutating: [add_issue_comment]", "mutating: []"
    )
    with pytest.raises(ConfigError, match=r"`allow` is empty"):
        config.load(write(tmp_path, body), environ={"GITHUB_TOKEN": "t"})


def test_an_empty_roles_list_is_a_typo_too(tmp_path: Path) -> None:
    body = VALID.replace("roles: [coder, debugger]", "roles: []")
    with pytest.raises(ConfigError, match=r"`roles` is empty"):
        config.load(write(tmp_path, body), environ={"GITHUB_TOKEN": "t"})


def test_an_unknown_role_names_the_ones_that_exist(tmp_path: Path) -> None:
    body = VALID.replace("roles: [coder, debugger]", "roles: [codr]")
    with pytest.raises(ConfigError, match=r"unknown roles"):
        config.load(write(tmp_path, body), environ={"GITHUB_TOKEN": "t"})


def test_an_unsupported_transport_says_which_are(tmp_path: Path) -> None:
    body = VALID.replace("transport: stdio", "transport: sse")
    with pytest.raises(ConfigError, match=r"stdio"):
        config.load(write(tmp_path, body), environ={"GITHUB_TOKEN": "t"})


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("servers: [a, b]\n", "must be a mapping"),
        ("servers:\n  github: 3\n", "must be a mapping"),
        (
            "servers:\n  GitHub:\n    command: [x]\n    allow: [y]\n    roles: [coder]\n",
            "name must be",
        ),
        ("servers:\n  github:\n    command: []\n", "non-empty list"),
        ("servers:\n  github:\n    command: 'npx'\n", "non-empty list"),
        ("- not a mapping\n", "must be a mapping"),
        ("servers:\n  github:\n    command: [x]\n    allow: 'get_issue'\n", "list of strings"),
    ],
)
def test_malformed_files_are_named_rather_than_crashed_on(
    tmp_path: Path, body: str, match: str
) -> None:
    with pytest.raises(ConfigError, match=match):
        config.load(write(tmp_path, body), environ={})


def test_invalid_yaml_says_it_is_invalid_yaml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"not valid YAML"):
        config.load(write(tmp_path, "servers:\n  github:\n   - [unclosed\n"), environ={})


def test_the_shipped_example_is_a_configuration_this_build_accepts() -> None:
    """The example is documentation people copy. One that the loader rejects is worse than
    none, and this is the sort of thing that rots the first time a rule is added."""
    example = Path(__file__).resolve().parents[2] / "mcp_servers.yaml.example"
    servers = config.load(
        example, environ={"GITHUB_TOKEN": "ghp_x", "TARGET_DATABASE_URL": "postgres://x"}
    )
    assert {s.name for s in servers} == {"github", "postgres"}


# ---- the four gaps a mutation sweep found in this module -----------------------------------
#
# `test_a_valid_file_parses` asserts `command[0] == "npx"` and stops there, which is how
# three of these survived: the first element of argv is the one thing that cannot go wrong
# without the server failing loudly.


def test_every_element_of_the_command_survives_parsing(tmp_path: Path) -> None:
    """`command[0]` is the binary; the rest is what it runs.

    `["npx", "-y", "@modelcontextprotocol/server-github"]` reduced to `["npx"]` launches
    npx's interactive prompt instead of the server, on a subprocess nobody is watching —
    so the mount hangs rather than failing, and the run waits on a session that will never
    come up. Keeping only the first element survived the whole suite, because the only
    assertion anywhere was on the first element.
    """
    [github] = config.load(write(tmp_path, VALID), environ={"GITHUB_TOKEN": "ghp_xyz"})
    assert github.command == ["npx", "-y", "@modelcontextprotocol/server-github"]


def test_a_configured_timeout_is_the_one_used(tmp_path: Path) -> None:
    """Parsed since this module was written and observed by nothing.

    A server that is slow to start is the reason the knob exists, so a deployment that
    raises it and silently keeps the 60-second default gets the failure it was trying to
    configure away — and no indication that its setting was ignored.
    """
    body = VALID.replace("    transport: stdio\n", "    transport: stdio\n    timeout_s: 180\n")
    [github] = config.load(write(tmp_path, body), environ={"GITHUB_TOKEN": "ghp_xyz"})
    assert github.timeout_s == 180


def test_an_unconfigured_timeout_falls_back_to_the_default(tmp_path: Path) -> None:
    """So the test above cannot pass on a parser that hard-codes 180."""
    [github] = config.load(write(tmp_path, VALID), environ={"GITHUB_TOKEN": "ghp_xyz"})
    assert github.timeout_s == config.TIMEOUT_S


def test_a_command_element_that_is_not_a_string_is_refused(tmp_path: Path) -> None:
    """The guard is invisible from the outside, because `str(c)` coerces behind it.

    Disarmed, `command: ["npx", 8080]` becomes `["npx", "8080"]` and runs — a port number
    handed to a launcher as an argument, which fails somewhere that names neither this file
    nor the line in the YAML. A config error caught at parse time is worth a great deal
    more than a subprocess that dies strangely.
    """
    body = VALID.replace(
        '    command: ["npx", "-y", "@modelcontextprotocol/server-github"]',
        '    command: ["npx", 8080]',
    )
    with pytest.raises(ConfigError, match="non-empty list of strings"):
        config.load(write(tmp_path, body), environ={"GITHUB_TOKEN": "ghp_xyz"})


def test_an_env_value_that_is_not_a_string_is_refused(tmp_path: Path) -> None:
    """Both halves of `not isinstance(key, str) or not isinstance(value, str)`.

    Dropping the value half survived the suite, because every test here used a string
    value. A numeric one reaches `expand`, which expects text — and the env of a
    subprocess has to be strings either way, so this fails later and less clearly.
    """
    body = VALID.replace(
        '      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"', "      PORT: 8080"
    )
    with pytest.raises(ConfigError, match="keys and values must be strings"):
        config.load(write(tmp_path, body), environ={"GITHUB_TOKEN": "ghp_xyz"})


def test_an_env_key_that_is_not_a_string_is_refused(tmp_path: Path) -> None:
    """The other half, so neither side of the `or` can go dead unnoticed."""
    body = VALID.replace('      GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"', '      8080: "x"')
    with pytest.raises(ConfigError, match="keys and values must be strings"):
        config.load(write(tmp_path, body), environ={"GITHUB_TOKEN": "ghp_xyz"})
