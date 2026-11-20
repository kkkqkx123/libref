"""C queries for declarations, macros, preprocessor conditions and calls."""

from __future__ import annotations


def entity_query() -> str:
    return r"""
(function_definition
  declarator: (function_declarator
    declarator: (identifier) @entity.function.name
    parameters: (parameter_list) @entity.function.params)
  body: (compound_statement) @entity.function.body) @entity.function

(function_definition
  type: (_) @entity.function.return_type
  declarator: (pointer_declarator
    declarator: (function_declarator
      declarator: (identifier) @entity.function.name
      parameters: (parameter_list) @entity.function.params))
  body: (compound_statement) @entity.function.body) @entity.function

(declaration
  declarator: (function_declarator
    declarator: (identifier) @entity.function.name
    parameters: (parameter_list) @entity.function.params)) @entity.function

(declaration
  type: (_) @entity.function.return_type
  declarator: (pointer_declarator
    declarator: (function_declarator
      declarator: (identifier) @entity.function.name
      parameters: (parameter_list) @entity.function.params))) @entity.function

(declaration
  type: (_) @entity.function.type
  declarator: (pointer_declarator
    declarator: (function_declarator
      declarator: (identifier) @entity.function.name
      parameters: (parameter_list) @entity.function.params))) @entity.function

(struct_specifier
  name: (type_identifier) @entity.struct.name
  body: (field_declaration_list) @entity.struct.body) @entity.struct

(union_specifier
  name: (type_identifier) @entity.union.name
  body: (field_declaration_list) @entity.union.body) @entity.union

(enum_specifier
  name: (type_identifier) @entity.enum.name
  body: (enumerator_list) @entity.enum.body) @entity.enum

(type_definition
  declarator: (type_identifier) @entity.type.name) @entity.type

(field_declaration
  type: (_) @entity.field.type
  declarator: (field_identifier) @entity.field.name) @entity.field

(preproc_def
  name: (identifier) @entity.macro.name) @entity.macro

(preproc_function_def
  name: (identifier) @entity.macro.name
  parameters: (preproc_params) @entity.macro.params
  value: (preproc_arg) @entity.macro.body) @entity.macro
"""


def call_query() -> str:
    return r"""
(call_expression
  function: (identifier) @call.function.name
  arguments: (argument_list) @call.function.arguments) @call.function

(call_expression
  function: (field_expression
    argument: (_) @call.method.object
    field: (field_identifier) @call.method.function)
  arguments: (argument_list) @call.method.arguments) @call.method

(call_expression
  function: (field_expression
    argument: (_) @call.method.object
    field: (field_identifier) @call.method.chained.to)
  arguments: (argument_list) @call.method.arguments) @call.chained

(call_expression
  function: (pointer_expression
    argument: (identifier) @call.pointer.name)
  arguments: (argument_list) @call.pointer.arguments) @call.pointer

(call_expression
  function: (parenthesized_expression
    (pointer_expression
      argument: (identifier) @call.pointer.name))
  arguments: (argument_list) @call.pointer.arguments) @call.pointer

(initializer_list
  (identifier) @call.funcref.name) @call.funcref

(initializer_pair
  value: (identifier) @call.funcref.name) @call.funcref

(assignment_expression
  right: (identifier) @call.funcref.name) @call.funcref

(assignment_expression
  right: (pointer_expression
    argument: (identifier) @call.funcref.name)) @call.funcref

(init_declarator
  value: (identifier) @call.funcref.name) @call.funcref
"""


def dependency_query() -> str:
    return r"""
(preproc_include
  path: (system_lib_string) @dependency.import.path) @dependency.import

(preproc_include
  path: (string_literal) @dependency.import.path) @dependency.import

(preproc_ifdef
  name: (identifier) @dependency.macro.ifdef.name) @dependency.macro.ifdef

(preproc_ifdef
  name: (identifier) @dependency.macro.ifndef.name) @dependency.macro.ifndef

(preproc_if
  condition: (preproc_defined
    (identifier) @dependency.macro.if.name)) @dependency.macro.if
"""


def comment_query() -> str:
    return r"""
(comment) @comment.doc
"""
