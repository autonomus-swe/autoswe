"""Go: funcs, the methods hung off a receiver, and named types."""

from __future__ import annotations

NAME = "go"

DEFINITIONS = """
(function_declaration name: (identifier) @name) @def
(method_declaration name: (field_identifier) @name) @def
(type_declaration (type_spec name: (type_identifier) @name)) @def
"""

REFERENCES = """
(call_expression function: (identifier) @ref)
(call_expression function: (selector_expression field: (field_identifier) @ref))
(selector_expression field: (field_identifier) @ref)
(type_identifier) @ref
"""

KINDS = {
    "function_declaration": "function",
    "method_declaration": "method",
    "type_declaration": "type",
}
