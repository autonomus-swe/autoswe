"""JavaScript: declarations, classes, methods, and the arrow functions assigned to consts."""

from __future__ import annotations

NAME = "javascript"

# The last two patterns are why this is not just `function_declaration`: most modern
# JavaScript defines its functions as `const handler = (req) => …`, and an index that
# missed those would miss the file's whole surface.
DEFINITIONS = """
(function_declaration name: (identifier) @name) @def
(generator_function_declaration name: (identifier) @name) @def
(class_declaration name: (identifier) @name) @def
(method_definition name: (property_identifier) @name) @def
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
"""

KINDS = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "method_definition": "method",
    "lexical_declaration": "const",
    "export_statement": "const",
}
