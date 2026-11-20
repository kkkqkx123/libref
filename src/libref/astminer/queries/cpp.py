"""C++ queries: the C query set plus class, member and qualified-call shapes."""

from __future__ import annotations

from libref.astminer.queries import c as _c


def entity_query() -> str:
    return (
        _c.entity_query()
        + r"""
(class_specifier
  name: (type_identifier) @entity.class.name
  body: (field_declaration_list) @entity.class.body) @entity.class

(function_definition
  declarator: (function_declarator
    declarator: (field_identifier) @entity.method.name
    parameters: (parameter_list) @entity.method.params)
  body: (compound_statement) @entity.method.body) @entity.method

(declaration
  declarator: (function_declarator
    declarator: (field_identifier) @entity.method.name
    parameters: (parameter_list) @entity.method.params)) @entity.method

(field_declaration
  type: (_) @entity.method.return_type
  declarator: (function_declarator
    declarator: (field_identifier) @entity.method.name
    parameters: (parameter_list) @entity.method.params)) @entity.method

(function_definition
  type: (_) @entity.method.return_type
  declarator: (function_declarator
    declarator: (qualified_identifier
      scope: (_) @entity.method.receiver
      name: (identifier) @entity.method.name)
    parameters: (parameter_list) @entity.method.params)
  body: (compound_statement) @entity.method.body) @entity.method

(declaration
  declarator: (function_declarator
    declarator: (identifier) @entity.constructor.name
    parameters: (parameter_list) @entity.constructor.params)) @entity.constructor

(declaration
  declarator: (function_declarator
    declarator: (destructor_name) @entity.constructor.name
    parameters: (parameter_list) @entity.constructor.params)) @entity.constructor

(function_definition
  type: (_) @entity.constructor.return_type
  declarator: (function_declarator
    declarator: (identifier) @entity.constructor.name
    parameters: (parameter_list) @entity.constructor.params)
  body: (compound_statement) @entity.constructor.body) @entity.constructor

(function_definition
  declarator: (function_declarator
    declarator: (destructor_name) @entity.constructor.name
    parameters: (parameter_list) @entity.constructor.params)
  body: (compound_statement) @entity.constructor.body) @entity.constructor

(namespace_definition
  name: (namespace_identifier) @entity.module.name
  body: (declaration_list) @entity.module.body) @entity.module
"""
    )


def call_query() -> str:
    return (
        _c.call_query()
        + r"""
(call_expression
  function: (qualified_identifier
    scope: (_) @call.method.object
    name: (identifier) @call.method.function)
  arguments: (argument_list) @call.method.arguments) @call.method

(call_expression
  function: (field_expression
    argument: (_) @call.method.object
    field: (field_identifier) @call.method.function)
  arguments: (argument_list) @call.method.arguments) @call.method

(new_expression
  type: (type_identifier) @call.constructor.name) @call.constructor
"""
    )


def dependency_query() -> str:
    return _c.dependency_query()


def comment_query() -> str:
    return _c.comment_query()
