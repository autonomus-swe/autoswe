"""Base classes that encode the two-model rule (see PHASES.md).

``LLMModel``  - a schema the model must produce. Strict, flat, no runtime state.
``StateModel`` - runtime state the orchestrator owns. May have defaults and methods.

Both carry ``__test__ = False`` so pytest never collects classes such as ``TestReport``.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class LLMModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    __test__: ClassVar[bool] = False


class StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    __test__: ClassVar[bool] = False
