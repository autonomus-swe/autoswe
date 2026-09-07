from __future__ import annotations

from contracts.common import LLMModel


class RepoProfile(LLMModel):
    """Produced by the Analyzer agent (README §6)."""

    languages: list[str]
    framework: str | None
    package_manager: str
    test_command: str
    lint_command: str | None
    conventions: list[str]
    entry_points: list[str]
