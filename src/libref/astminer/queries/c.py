"""C queries for functions, structs, includes, macro-like calls and comments."""

from __future__ import annotations


def entity_query() -> str:
    return r"""
(function_definition
  declarator: (function_declarator
    declarator: (identifier) @entity.function.name
    parameters: (parameter_list) @entity.function.params)
  body: (compound_statement) @entity.function.body) @entity.function

(declaration
  declarator: (function_declarator
    declarator: (identifier) @entity.function.name
    parameters: (parameter_list) @entity.function.params)) @entity.function

(struct_specifier
  name: (type_identifier) @entity.struct.name
  body: (field_declaration_list) @entity.struct.body) @entity.struct

(enum_specifier
  name: (type_identifier) @entity.enum.name
  body: (enumerator_list) @entity.enum.body) @entity.enum
"""


def call_query() -> str:
    return r"""
(call_expression
  function: (identifier) @call.function.name
  arguments: (argument_list) @call.function.arguments) @call.function

(call_expression
  function: (field_expression
    field: (field_identifier) @call.method.function)
  arguments: (argument_list) @call.method.arguments) @call.method
"""


def dependency_query() -> str:
    return r"""
(preproc_include
  path: (system_lib_string) @dependency.import.path) @dependency.import

(preproc_include
  path: (string_literal) @dependency.import.path) @dependency.import
"""


def comment_query() -> str:
    return r"""
(comment) @comment.doc
"""
