"""Kotlin queries: pattern text for the four domains of entities, calls, dependencies, and comments.

Targets the vendored fwcd grammar: class-likes share the class_declaration node
with the kind keyword kept as an anonymous child, names are simple_identifier
except declarations which use type_identifier, calls wrap arguments in
call_suffix, member access nests through navigation_suffix, comments split into
line and multiline nodes.
"""

from __future__ import annotations


def entity_query() -> str:
    return r"""
(class_declaration
  (type_identifier) @entity.class.name @entity.class.signature.name
  (type_parameters)? @entity.class.signature.type_params
  (delegation_specifier
    [
      (user_type) @entity.class.signature.base
      (explicit_delegation (user_type) @entity.class.signature.base)
      (constructor_invocation (user_type) @entity.class.signature.base)
    ])?
  (class_body)? @entity.class.body) @entity.class

(object_declaration
  (type_identifier) @entity.class.name @entity.class.signature.name
  (class_body)? @entity.class.body) @entity.class

(companion_object
  (type_identifier)? @entity.class.name @entity.class.signature.name
  (class_body)? @entity.class.body) @entity.class

(function_declaration
  (simple_identifier) @entity.function.name @entity.function.signature.name
  (type_parameters)? @entity.function.signature.type_params
  (function_value_parameters) @entity.function.params @entity.function.signature.params
  [(user_type) (nullable_type)]? @entity.function.return_type @entity.function.signature.return_type
  (function_body)? @entity.function.body) @entity.function

(secondary_constructor
  ("constructor") @entity.constructor.name @entity.constructor.signature.name
  (function_value_parameters) @entity.constructor.params @entity.constructor.signature.params
  (statements)? @entity.constructor.body) @entity.constructor

(class_body
  (property_declaration
    (variable_declaration
      (simple_identifier) @entity.field.name @entity.field.signature.name)) @entity.field)

(class_declaration
  (primary_constructor
    (class_parameter
      (binding_pattern_kind "val")
      (simple_identifier) @entity.field.name @entity.field.signature.name
      (user_type)? @entity.field.type @entity.field.signature.type) @entity.field))

(class_declaration
  (primary_constructor
    (class_parameter
      (binding_pattern_kind "var")
      (simple_identifier) @entity.field.name @entity.field.signature.name
      (user_type)? @entity.field.type @entity.field.signature.type) @entity.field))

(enum_class_body
  (enum_entry
    (simple_identifier) @entity.enum_constant.name @entity.enum_constant.signature.name) @entity.enum_constant)

(type_alias
  (type_identifier) @entity.type.name @entity.type.signature.name) @entity.type

(package_header
  (identifier) @entity.package.name) @entity.package
"""


def call_query() -> str:
    return r"""
(call_expression
  (simple_identifier) @call.function.name
  (call_suffix
    (value_arguments)? @call.function.arguments)) @call.function

(call_expression
  (navigation_expression
    (_) @call.method.object
    (navigation_suffix
      "." (simple_identifier) @call.method.function))
  (call_suffix
    (value_arguments)? @call.method.arguments)) @call.method

(call_expression
  [(simple_identifier) (navigation_expression)] @call.hof.method.name
  (call_suffix
    (annotated_lambda) @call.hof.callback)) @call.hof
"""


def dependency_query() -> str:
    return r"""
(import_header
  (identifier) @dependency.import.name) @dependency.import

(import_header
  (wildcard_import) @dependency.import.wildcard) @dependency.import.wildcard

(import_header
  (identifier) @dependency.import.name
  (import_alias
    (type_identifier) @dependency.import.alias.name)) @dependency.import.alias
"""


def comment_query() -> str:
    return r"""
(line_comment) @comment.line

(multiline_comment) @comment.doc
"""
