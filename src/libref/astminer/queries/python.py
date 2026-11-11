"""Python queries: pattern text for the four domains of entities, calls, dependencies, and comments."""

from __future__ import annotations


def entity_query() -> str:
    return r"""
(class_definition
  name: (identifier) @entity.class.name @entity.class.signature.name
  superclasses: (argument_list
    [(identifier) @entity.class.base @entity.class.signature.base
     (attribute) @entity.class.base @entity.class.signature.base
     (keyword_argument value: (_) @entity.class.base @entity.class.signature.base)])?
  body: (block) @entity.class.body) @entity.class

(function_definition
  name: (identifier) @entity.function.name @entity.function.signature.name
  parameters: (parameters) @entity.function.params @entity.function.signature.params
  return_type: [(type) (string)]? @entity.function.return_type @entity.function.signature.return_type
  body: (block) @entity.function.body) @entity.function

(class_definition
  body: (block
    (function_definition
      name: (identifier) @entity.method.name @entity.method.signature.name
      parameters: (parameters) @entity.method.params @entity.method.signature.params
      return_type: [(type) (string)]? @entity.method.return_type @entity.method.signature.return_type
      body: (block) @entity.method.body) @entity.method))

(decorator
  (identifier) @entity.decorator.name) @entity.decorator

(decorator
  (call
    function: (identifier) @entity.decorator.call.name
    arguments: (argument_list) @entity.decorator.call.arguments)) @entity.decorator

(decorator
  (call
    function: (attribute) @entity.decorator.call.name
    arguments: (argument_list) @entity.decorator.call.arguments)) @entity.decorator

(expression_statement
  (assignment
    left: (attribute
      object: (_) @entity.field.receiver
      attribute: (identifier) @entity.field.name)
    right: (_) @entity.field.value)) @entity.field
"""


def call_query() -> str:
    return r"""
(call
  function: (identifier) @call.function.name
  arguments: (argument_list)? @call.function.arguments) @call.function

(call
  function: (attribute
    object: (_) @call.method.object
    attribute: (identifier) @call.method.function)
  arguments: (argument_list)? @call.method.arguments) @call.method

(call
  function: (attribute
    object: (call) @call.method.chained.from
    attribute: (identifier) @call.method.chained.to)) @call.method.chained

(call
  function: (attribute
    object: (call
      function: (identifier) @call.super.method.class)
    attribute: (identifier) @call.super.method.name)) @call.super.method

(await
  (call
    function: (identifier) @call.async.function.name)) @call.async

(await
  (call
    function: (attribute
      object: (_) @call.async.method.object
      attribute: (identifier) @call.async.method.name))) @call.async.method
"""


def dependency_query() -> str:
    return r"""
(import_statement
  name: (dotted_name) @dependency.import.module) @dependency.import

(import_statement
  name: (aliased_import
    name: (dotted_name) @dependency.import.alias.path
    alias: (identifier) @dependency.import.alias.name)) @dependency.import.alias

(import_from_statement
  module_name: (dotted_name) @dependency.import.from.module) @dependency.import.from

(import_from_statement
  module_name: (relative_import) @dependency.import.relative) @dependency.import.relative

(wildcard_import) @dependency.import.wildcard

(future_import_statement
  name: (dotted_name) @dependency.import.future.module) @dependency.import.future
"""


def comment_query() -> str:
    return r"""
(comment) @comment.line

(expression_statement
  (string
    (string_content) @comment.doc))
"""
