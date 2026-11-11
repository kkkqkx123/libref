"""Kotlin queries: pattern text for the four domains of entities, calls, dependencies, and comments."""

from __future__ import annotations


def entity_query() -> str:
    return r"""
(class_declaration
  (identifier) @entity.class.name @entity.class.signature.name
  (type_parameters)? @entity.class.signature.type_params
  (delegation_specifiers
    (delegation_specifier
      (user_type) @entity.class.signature.base))?
  (class_body)? @entity.class.body) @entity.class

(object_declaration
  (identifier) @entity.class.name @entity.class.signature.name
  (class_body)? @entity.class.body) @entity.class

(companion_object
  (identifier) @entity.class.name @entity.class.signature.name
  (class_body)? @entity.class.body) @entity.class

(function_declaration
  (identifier) @entity.function.name @entity.function.signature.name
  (type_parameters)? @entity.function.signature.type_params
  (function_value_parameters) @entity.function.params @entity.function.signature.params
  [(user_type) (nullable_type)]? @entity.function.return_type @entity.function.signature.return_type
  (function_body)? @entity.function.body) @entity.function

(class_body
  (property_declaration
    (variable_declaration
      (identifier) @entity.field.name @entity.field.signature.name)) @entity.field)

(package_header
  (qualified_identifier) @entity.package.name) @entity.package
"""


def call_query() -> str:
    return r"""
(call_expression
  [(identifier) (navigation_expression)] @call.function.name
  [(value_arguments) (annotated_lambda)]? @call.function.arguments) @call.function

(call_expression
  [(identifier) (navigation_expression)] @call.hof.method.name
  (annotated_lambda
    (lambda_literal) @call.hof.callback)) @call.hof
"""


def dependency_query() -> str:
    return r"""
(import
  (qualified_identifier) @dependency.import.name) @dependency.import
"""


def comment_query() -> str:
    return r"""
(line_comment) @comment.line

(block_comment) @comment.doc
"""
