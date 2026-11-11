"""Groovy queries: pattern text for the four domains of entities, calls, dependencies, and comments.

Targets the vendored murtaza64 grammar: classes own their body closure, methods
are function definitions nested in that closure, fields and locals share the
declaration node with name/type/value fields, and calls split into parenthesized
function calls plus juxtaposed command calls. Block and line comments share the
comment node, so both feed line spans.
"""

from __future__ import annotations


def entity_query() -> str:
    return r"""
(class_definition
  name: (identifier) @entity.class.name @entity.class.signature.name
  [(identifier) (qualified_name) (dotted_identifier)]? @entity.class.signature.base
  body: [(closure) (enum_body)] @entity.class.body) @entity.class

(class_definition
  body: (closure
    (function_definition
      function: (identifier) @entity.method.name @entity.method.signature.name
      parameters: (parameter_list) @entity.method.params @entity.method.signature.params
      body: (closure)? @entity.method.body) @entity.method))

(class_definition
  body: (enum_body
    (function_definition
      function: (identifier) @entity.method.name @entity.method.signature.name
      parameters: (parameter_list) @entity.method.params @entity.method.signature.params
      body: (closure)? @entity.method.body) @entity.method))

(enum_body
  (enum_constants
    (enum_constant
      name: (identifier) @entity.enum_constant.name @entity.enum_constant.signature.name) @entity.enum_constant))

(source_file
  (function_definition
    function: (identifier) @entity.function.name @entity.function.signature.name
    parameters: (parameter_list) @entity.function.params @entity.function.signature.params
    body: (closure)? @entity.function.body) @entity.function)

(class_definition
  body: (closure
    (declaration
      name: (identifier) @entity.field.name @entity.field.signature.name) @entity.field))

(class_definition
  body: (enum_body
    (declaration
      name: (identifier) @entity.field.name @entity.field.signature.name) @entity.field))

(function_definition
  body: (closure
    (declaration
      name: (identifier) @entity.variable.name @entity.variable.signature.name) @entity.variable))

(groovy_package
  (qualified_name) @entity.package.name) @entity.package
"""


def call_query() -> str:
    return r"""
(function_call
  function: [(identifier) (dotted_identifier)] @call.function.name
  (argument_list)? @call.function.arguments) @call.function

(function_call
  function: [(identifier) (dotted_identifier)] @call.hof.method.name
  (argument_list
    (closure) @call.hof.callback)) @call.hof

(juxt_function_call
  [(identifier) (dotted_identifier)] @call.function.name
  (argument_list)? @call.function.arguments) @call.function

(juxt_function_call
  [(identifier) (dotted_identifier)] @call.hof.method.name
  (argument_list
    (closure) @call.hof.callback)) @call.hof
"""


def dependency_query() -> str:
    return r"""
(groovy_import
  (qualified_name) @dependency.import.name) @dependency.import

(groovy_import
  (wildcard_import) @dependency.import.wildcard) @dependency.import.wildcard
"""


def comment_query() -> str:
    return r"""
(comment) @comment.line

((comment) @comment.doc
  (#match? @comment.doc "^(/\\*\\*|///)"))
"""
