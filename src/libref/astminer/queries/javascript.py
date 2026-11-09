"""JavaScript queries: pattern text for the four domains of entities, calls, dependencies, and comments."""

from __future__ import annotations


def entity_query() -> str:
    return r"""
(class_declaration
  name: (identifier) @entity.class.name @entity.class.signature.name
  (class_heritage
    [(identifier) @entity.class.base @entity.class.signature.base
     (member_expression) @entity.class.base @entity.class.signature.base])?
  body: (class_body) @entity.class.body) @entity.class

(function_declaration
  name: (identifier) @entity.function.name @entity.function.signature.name
  parameters: (formal_parameters) @entity.function.params @entity.function.signature.params
  body: (statement_block) @entity.function.body) @entity.function

(generator_function_declaration
  name: (identifier) @entity.generator.name @entity.generator.signature.name
  parameters: (formal_parameters) @entity.generator.params @entity.generator.signature.params
  body: (statement_block) @entity.generator.body) @entity.generator

(class_body
  (method_definition
    name: (property_identifier) @entity.method.name @entity.method.signature.name
    parameters: (formal_parameters) @entity.method.params @entity.method.signature.params
    body: (statement_block)? @entity.method.body) @entity.method)

(object
  (method_definition
    name: (property_identifier) @entity.object_method.name @entity.object_method.signature.name
    parameters: (formal_parameters) @entity.object_method.params @entity.object_method.signature.params
    body: (statement_block) @entity.object_method.body) @entity.object_method)

(pair
  key: (property_identifier) @entity.arrow.name @entity.arrow.signature.name
  value: (arrow_function
    parameters: (formal_parameters) @entity.arrow.params @entity.arrow.signature.params
    body: (_) @entity.arrow.body)) @entity.arrow

(variable_declarator
  name: (identifier) @entity.arrow.name @entity.arrow.signature.name
  value: (arrow_function
    parameters: (formal_parameters) @entity.arrow.params @entity.arrow.signature.params
    body: (_) @entity.arrow.body)) @entity.arrow

(variable_declarator
  name: (identifier) @entity.variable.name
  value: (_) @entity.variable.value) @entity.variable
"""


def call_query() -> str:
    return r"""
(call_expression
  function: (identifier) @call.function.name
  arguments: (arguments)? @call.function.arguments) @call.function

(call_expression
  function: (member_expression
    object: (_) @call.method.object
    property: (property_identifier) @call.method.function)
  arguments: (arguments)? @call.method.arguments) @call.method

(new_expression
  constructor: (_) @call.constructor.name
  arguments: (arguments)? @call.constructor.arguments) @call.constructor
"""


def dependency_query() -> str:
    return r"""
(import_statement
  (import_clause
    (identifier)? @dependency.import.default.name
    (named_imports
      (import_specifier) @dependency.import.named.spec)?)?
  source: (string) @dependency.import.module) @dependency.import

(import_statement
  (import_clause
    (namespace_import
      (identifier) @dependency.import.namespace.name))
  source: (string) @dependency.import.module) @dependency.import.namespace

(variable_declarator
  name: (identifier) @dependency.require.name
  value: (call_expression
    function: (identifier) @dependency.require.callee
    arguments: (arguments
      (string) @dependency.require.module))) @dependency.require

(variable_declarator
  name: (object_pattern) @dependency.require.destruct
  value: (call_expression
    function: (identifier) @dependency.require.callee
    arguments: (arguments
      (string) @dependency.require.module))) @dependency.require.destruct

(export_statement
  (export_clause)
  source: (string) @dependency.export.module) @dependency.export
"""


def comment_query() -> str:
    return r"""
(comment) @comment.line
"""
