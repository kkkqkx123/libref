"""Go queries: entity, call, dependency, and comment patterns.

Subset of the validated code-context-engine scheme, renamed to the engine
capture contract. Import specs match unanchored so grouped imports hit;
embedding feeds extend bases; goroutine and deferred statements are call
roots handled by the engine node-type set.
"""

from __future__ import annotations


def entity_query() -> str:
    return r"""
(type_declaration
  (type_spec
    name: (type_identifier) @entity.type.name @entity.type.signature.name)) @entity.type

(type_declaration
  (type_spec
    name: (type_identifier) @entity.struct.name @entity.struct.signature.name
    type_parameters: (_)? @entity.struct.signature.type_params
    type: (struct_type) @entity.struct.body)) @entity.struct

(type_declaration
  (type_spec
    name: (type_identifier) @entity.interface.name @entity.interface.signature.name
    type_parameters: (_)? @entity.interface.signature.type_params
    type: (interface_type) @entity.interface.body)) @entity.interface

(interface_type
  (method_elem
    name: (field_identifier) @entity.method.name @entity.method.signature.name
    parameters: (parameter_list) @entity.method.params @entity.method.signature.params
    result: (_)? @entity.method.return_type @entity.method.signature.return_type)) @entity.method

(function_declaration
  name: (identifier) @entity.function.name @entity.function.signature.name
  type_parameters: (_)? @entity.function.signature.type_params
  parameters: (parameter_list) @entity.function.params @entity.function.signature.params
  result: (_)? @entity.function.return_type @entity.function.signature.return_type
  body: (block) @entity.function.body) @entity.function

(method_declaration
  receiver: (parameter_list) @entity.method.receiver
  name: (field_identifier) @entity.method.name @entity.method.signature.name
  parameters: (parameter_list) @entity.method.params @entity.method.signature.params
  result: (_)? @entity.method.return_type @entity.method.signature.return_type
  body: (block) @entity.method.body) @entity.method

(short_var_declaration
  left: (expression_list
    (identifier) @entity.function.name @entity.function.signature.name)
  right: (expression_list
    (func_literal) @entity.function.params)) @entity.function

(var_declaration
  (var_spec
    name: (identifier) @entity.variable.name
    type: (_)? @entity.variable.type
    value: (_)? @entity.variable.value)) @entity.variable

(short_var_declaration
  left: (expression_list
    (identifier) @entity.variable.name)
  right: (_) @entity.variable.value) @entity.variable

(const_declaration
  (const_spec
    name: (identifier) @entity.variable.name
    type: (_)? @entity.variable.type
    value: (_)? @entity.variable.value)) @entity.variable

(for_statement
  (range_clause
    left: (expression_list
      (identifier) @entity.variable.name @entity.variable.signature.name)
    right: (_) @entity.variable.source @entity.variable.signature.source)) @entity.variable

(field_declaration
  name: (field_identifier) @entity.field.name
  type: (_) @entity.field.type) @entity.field

(field_declaration
  name: (field_identifier) @entity.field.name
  type: (_) @entity.field.type
  tag: (raw_string_literal) @entity.field.tag) @entity.field

(package_clause
  (package_identifier) @entity.package.name) @entity.package
"""


def call_query() -> str:
    return r"""
(call_expression
  function: (identifier) @call.function.name
  arguments: (argument_list)? @call.function.arguments) @call.function

(call_expression
  function: (selector_expression
    operand: (_) @call.method.object
    field: (field_identifier) @call.method.function)
  arguments: (argument_list)? @call.method.arguments) @call.method

(call_expression
  function: (selector_expression
    operand: (call_expression) @call.method.chained.from
    field: (field_identifier) @call.method.chained.to)
  arguments: (argument_list)? @call.method.arguments) @call.method.chained

(go_statement
  (call_expression
    function: (identifier) @call.function.name
    arguments: (argument_list)? @call.function.arguments)) @call.function

(go_statement
  (call_expression
    function: (selector_expression
      operand: (_) @call.method.object
      field: (field_identifier) @call.method.function)
    arguments: (argument_list)? @call.method.arguments)) @call.method

(defer_statement
  (call_expression
    function: (identifier) @call.function.name
    arguments: (argument_list)? @call.function.arguments)) @call.function

(defer_statement
  (call_expression
    function: (selector_expression
      operand: (_) @call.method.object
      field: (field_identifier) @call.method.function)
    arguments: (argument_list)? @call.method.arguments)) @call.method
"""


def dependency_query() -> str:
    return r"""
(import_spec
  path: (interpreted_string_literal) @dependency.import.path) @dependency.import

(import_spec
  name: (_) @dependency.import.alias.name
  path: (interpreted_string_literal) @dependency.import.alias.path) @dependency.import.alias

(field_declaration
  !name
  type: (type_identifier) @dependency.extend.name) @dependency.embedding

(field_declaration
  !name
  type: (pointer_type
    (type_identifier) @dependency.extend.name)) @dependency.embedding

(field_declaration
  !name
  type: (qualified_type) @dependency.extend.name) @dependency.embedding

(interface_type
  (type_elem
    [(type_identifier) @dependency.extend.name
     (pointer_type
       (type_identifier) @dependency.extend.name)
     (qualified_type) @dependency.extend.name])) @dependency.embedding
"""


def comment_query() -> str:
    return r"""
(comment) @comment.line
"""
