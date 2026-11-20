"""C++ queries for functions, classes, includes, calls and comments."""

from __future__ import annotations


def entity_query() -> str:
    return r"""
(function_definition
  declarator: (function_declarator
    declarator: (identifier) @entity.function.name
    parameters: (parameter_list) @entity.function.params)
  body: (compound_statement) @entity.function.body) @entity.function

(function_definition
  declarator: (function_declarator
    declarator: (field_identifier) @entity.function.name
    parameters: (parameter_list) @entity.function.params)
  body: (compound_statement) @entity.function.body) @entity.function

(class_specifier
  name: (type_identifier) @entity.class.name
  body: (field_declaration_list) @entity.class.body) @entity.class

(struct_specifier
  name: (type_identifier) @entity.struct.name
  body: (field_declaration_list) @entity.struct.body) @entity.struct
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

(call_expression
  function: (qualified_identifier
    name: (identifier) @call.function.name)
  arguments: (argument_list) @call.function.arguments) @call.function
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
