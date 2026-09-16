"""One parser per scanner, each normalising to :class:`contracts.SecurityFinding`.

Four tools disagree about almost everything — what a severity is called, whether a finding
has a line, whether the same problem is reported once or twice — so each parser owns its
tool's shape and none of them leak it. The severity maps live beside the parser that needs
them, because "what does bandit's MEDIUM mean" is a fact about bandit.

Every parser here was written against output a real scanner produced, saved under
``tests/fixtures/scanners/``. That is not ceremony: generating them turned up two things no
hand-written sample would have. pip-audit reports every vulnerability **twice**, and its
JSON carries no severity at all — so "map by CVSS if present" means "always high" in
practice. Both are in the parsers, with the reason.
"""

from __future__ import annotations

from tools.scanner_parsers.bandit import parse_bandit
from tools.scanner_parsers.gitleaks import parse_gitleaks
from tools.scanner_parsers.pip_audit import parse_pip_audit
from tools.scanner_parsers.semgrep import parse_semgrep

__all__ = ["parse_bandit", "parse_gitleaks", "parse_pip_audit", "parse_semgrep"]
