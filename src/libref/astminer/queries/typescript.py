"""TypeScript queries: pattern text for the four domains of entities, calls, dependencies, and comments.

Targets the tree-sitter-typescript tsx grammar: class names are type
identifiers, heritage splits into extends and implements clauses, interfaces
and type aliases are standalone declarations, enums own their body, fields use
public field definitions, abstract members use signature nodes.
"""

from __future__ import annotations


def entity_query() -> str:
    return r"""
(class_declaration
  name: (type_identifier) @entity.class.name @entity.class.signature.name
  (type_parameters)? @entity.class.signature.type_params
  (class_heritage
    (extends_clause
      [(identifier) @entity.class.base @entity.class.signature.base
       (member_expression) @entity.class.base @entity.class.signature.base])?
    (implements_clause
      [(member_expression) @entity.class.base @entity.class.signature.base
       (generic_type) @entity.class.base @entity.class.signature.base
       (nested_type_identifier) @entity.class.base @entity.class.signature.base
       (type_identifier) @entity.class.base @entity.class.signature.base])?)?
  body: (class_body) @entity.class.body) @entity.class

(abstract_class_declaration
  name: (type_identifier) @entity.class.name @entity.class.signature.name
  (type_parameters)? @entity.class.signature.type_params
  (class_heritage
    (extends_clause
      [(identifier) @entity.class.base @entity.class.signature.base
       (member_expression) @entity.class.base @entity.class.signature.base])?
    (implements_clause
      [(member_expression) @entity.class.base @entity.class.signature.base
       (generic_type) @entity.class.base @entity.class.signature.base
       (nested_type_identifier) @entity.class.base @entity.class.signature.base
       (type_identifier) @entity.class.base @entity.class.signature.base])?)?
  body: (class_body) @entity.class.body) @entity.class

(interface_declaration
  name: (type_identifier) @entity.interface.name @entity.interface.signature.name
  (type_parameters)? @entity.interface.signature.type_params
  (extends_type_clause
    [(generic_type) @entity.interface.base @entity.interface.signature.base
     (nested_type_identifier) @entity.interface.base @entity.interface.signature.base
     (type_identifier) @entity.interface.base @entity.interface.signature.base])?
  body: (interface_body) @entity.interface.body) @entity.interface

(type_alias_declaration
  name: (type_identifier) @entity.type.name @entity.type.signature.name
  (type_parameters)? @entity.type.signature.type_params) @entity.type

(enum_declaration
  name: (identifier) @entity.enum.name @entity.enum.signature.name
  body: (enum_body) @entity.enum.body) @entity.enum

(enum_body
  (property_identifier) @entity.enum_constant @entity.enum_constant.name @entity.enum_constant.signature.name)

(enum_assignment
  name: (property_identifier) @entity.enum_constant.name @entity.enum_constant.signature.name) @entity.enum_constant

(function_declaration
  name: (identifier) @entity.function.name @entity.function.signature.name
  (type_parameters)? @entity.function.signature.type_params
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

(class_body
  (abstract_method_signature
    name: (property_identifier) @entity.method.name @entity.method.signature.name
    parameters: (formal_parameters) @entity.method.params @entity.method.signature.params) @entity.method)

(class_body
  (public_field_definition
    name: (property_identifier) @entity.field.name @entity.field.signature.name) @entity.field)

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

(call_expression
  function: (member_expression
    object: (call_expression) @call.method.chained.from
    property: (property_identifier) @call.method.chained.to)
  arguments: (arguments)? @call.method.arguments) @call.method.chained

(jsx_opening_element
  [(identifier) (member_expression)] @call.jsx.name) @call.jsx

(jsx_self_closing_element
  [(identifier) (member_expression)] @call.jsx.name) @call.jsx

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

(variable_declarator
  name: (identifier) @dependency.import.dynamic.name
  value: (call_expression
    function: (import)
    arguments: (arguments
      (string) @dependency.import.dynamic.module))) @dependency.import.dynamic

(variable_declarator
  name: (identifier) @dependency.import.dynamic.name
  value: (await_expression
    (call_expression
      function: (import)
      arguments: (arguments
        (string) @dependency.import.dynamic.module)))) @dependency.import.dynamic

(export_statement
  (export_clause)
  source: (string) @dependency.export.module) @dependency.export

(export_statement
  (export_clause
    (export_specifier) @dependency.export.spec)
  source: (string) @dependency.export.module) @dependency.export.named
"""


def comment_query() -> str:
    return r"""
(comment) @comment.line

((comment) @comment.doc
  (#match? @comment.doc "^/\\*\\*"))
"""
