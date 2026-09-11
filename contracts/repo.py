from __future__ import annotations

from pydantic import Field

from contracts.common import LLMModel, StateModel


class RepoProfile(LLMModel):
    """Produced by the Analyzer agent (README §6)."""

    languages: list[str]
    framework: str | None
    package_manager: str
    test_command: str
    lint_command: str | None
    conventions: list[str]
    entry_points: list[str]


class RepoFacts(StateModel):
    """What can be known about a repository without asking a model.

    Runtime state, never shown to a model as a schema — it is rendered as text into the
    Analyzer's prompt, which then confirms or corrects it.
    """

    languages: list[str] = Field(default_factory=list)
    package_manager: str | None = None
    install_command: str | None = None
    test_command: str | None = None
    lint_command: str | None = None
    python_version: str | None = None
    file_count: int = 0
    top_level: list[str] = Field(default_factory=list)
    manifests: list[str] = Field(default_factory=list)
    ci_test_lines: list[str] = Field(default_factory=list)
    readme_head: str = ""
    detected_by: str | None = None  # which rule produced test_command, for debugging

    def render(self) -> str:
        """Plain text for a prompt. Never JSON: this is context, not a contract."""
        rows = [
            ("languages", ", ".join(self.languages) or "unknown"),
            ("package manager", self.package_manager or "unknown"),
            ("install command", self.install_command or "none detected"),
            ("test command", self.test_command or "none detected"),
            ("lint command", self.lint_command or "none detected"),
            ("python version", self.python_version or "unspecified"),
            ("files", str(self.file_count)),
            ("manifests", ", ".join(self.manifests) or "none"),
            ("detected by", self.detected_by or "-"),
        ]
        out = [f"{k:17} {v}" for k, v in rows]
        if self.ci_test_lines:
            out.append("CI test lines:")
            out += [f"  {line}" for line in self.ci_test_lines]
        return "\n".join(out)
