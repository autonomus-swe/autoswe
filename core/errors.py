"""Shared exception hierarchy. Every later layer raises these, never bare Exception."""


class AutosweError(Exception):
    """Base class for all project errors."""


class ConfigError(AutosweError):
    """Invalid or missing configuration."""


class PolicyViolation(AutosweError):
    """A tool call was denied by policy (deny-list, path confinement, role restriction)."""


class SandboxError(AutosweError):
    """The sandbox could not be created, attached, or executed in."""


class BudgetExceeded(AutosweError):
    """A run exceeded a token, dollar, or wall-clock budget."""


class ProviderError(AutosweError):
    """An LLM provider returned an unusable response (refusal, malformed structured output)."""


class AgentError(AutosweError):
    """An agent finished without producing its required structured result."""
