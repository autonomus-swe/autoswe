"""`${VAR}` substitution for configuration files, with a missing variable as an error.

Two config files now want this — `mcp_servers.yaml` and `evals/tasks/*.yaml` — and both
want the same refusal. A `${GITHUB_TOKEN}` that silently becomes the empty string produces
a server that starts, answers `list_tools`, and fails on its first real call somewhere that
mentions neither the file nor the variable; a `${AUTOSWE_FIXTURE_REPO}` that does the same
produces an eval suite that reports every task as unresolved for a reason no row records.

Here rather than copied into each, because the second copy of a check is the one that
stops matching the first.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from core.errors import ConfigError

VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand(value: str, environ: Mapping[str, str], *, where: str) -> str:
    """Substitute `${VAR}` from `environ`. An unset or empty variable raises.

    `where` names the file and field, because the caller of a configuration loader is a
    person editing a file and the useful message says which line to look at.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if not environ.get(name):
            raise ConfigError(f"{where} references ${{{name}}}, which is not set")
        return environ[name]

    return VAR.sub(replace, value)
