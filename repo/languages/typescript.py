"""TypeScript: the JavaScript surface, plus the types that are the point of using it."""

from __future__ import annotations

NAME = "typescript"

DEFINITIONS = """
(function_declaration name: (identifier) @name) @def
(class_declaration name: (type_identifier) @name) @def
(method_definition name: (property_identifier) @name) @def
(interface_declaration name: (type_identifier) @name) @def
(type_alias_declaration name: (type_identifier) @name) @def
(enum_declaration name: (identifier) @name) @def
(lexical_declaration
  (variable_declarator
    name: (identifier) @name
    value: [(arrow_function) (function_expression)])) @def
(export_statement
  (lexical_declaration (variable_declarator name: (identifier) @name))) @def
"""

REFERENCES = """
(call_expression function: (identifier) @ref)
(call_expression function: (member_expression property: (property_identifier) @ref))
(member_expression property: (property_identifier) @ref)
(type_identifier) @ref
"""

KINDS = {
    "function_declaration": "function",
    "class_declaration": "class",
    "method_definition": "method",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "type",
    "lexical_declaration": "const",
    "export_statement": "const",
}
