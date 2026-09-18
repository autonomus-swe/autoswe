"""Python: functions, classes, and the methods inside them."""

from __future__ import annotations

NAME = "python"

# Both `function_definition` patterns catch methods too, because a method *is* a
# function_definition inside a class body. Which one it is cannot be expressed in the
# query, so `symbols.py` decides from the node's ancestors.
DEFINITIONS = """
(function_definition name: (identifier) @name) @def
(class_definition name: (identifier) @name) @def
"""

# `f(x)` and `obj.method(x)`, plus bare attribute access, which is how a module-level
# constant or a class attribute gets used.
REFERENCES = """
(call function: (identifier) @ref)
(call function: (attribute attribute: (identifier) @ref))
(attribute attribute: (identifier) @ref)
"""

KINDS = {"function_definition": "function", "class_definition": "class"}
