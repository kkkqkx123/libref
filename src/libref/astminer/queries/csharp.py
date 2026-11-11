"""C# queries: entity, call, dependency, and comment patterns.

Subset of the validated code-context-engine scheme, renamed to the engine
capture contract. Member access without a host constraint covers anonymous
`this` hosts; using directives match without field constraints so plain
usings hit, with alias detection left to the alias builder.
"""

from __future__ import annotations


def entity_query() -> str:
    return r"""
(class_declaration
  name: (identifier) @entity.class.name @entity.class.signature.name
  (type_parameter_list)? @entity.class.signature.type_params
  (base_list
    (_) @entity.class.base @entity.class.signature.base)?
  body: (declaration_list)? @entity.class.body) @entity.class

(interface_declaration
  name: (identifier) @entity.interface.name @entity.interface.signature.name
  body: (declaration_list)? @entity.interface.body) @entity.interface

(struct_declaration
  name: (identifier) @entity.struct.name @entity.struct.signature.name
  (type_parameter_list)? @entity.struct.signature.type_params
  body: (declaration_list)? @entity.struct.body) @entity.struct

(enum_declaration
  name: (identifier) @entity.enum.name @entity.enum.signature.name) @entity.enum

(enum_member_declaration
  name: (identifier) @entity.enum_constant.name @entity.enum_constant.signature.name) @entity.enum_constant

(record_declaration
  name: (identifier) @entity.record.name @entity.record.signature.name
  (type_parameter_list)? @entity.record.signature.type_params
  body: (declaration_list)? @entity.record.body) @entity.record

(method_declaration
  returns: (_)? @entity.method.return_type @entity.method.signature.return_type
  name: (identifier) @entity.method.name @entity.method.signature.name
  parameters: (parameter_list) @entity.method.params @entity.method.signature.params
  body: (_)? @entity.method.body) @entity.method

(constructor_declaration
  name: (identifier) @entity.constructor.name @entity.constructor.signature.name
  (parameter_list) @entity.constructor.params @entity.constructor.signature.params
  (block)? @entity.constructor.body) @entity.constructor

(destructor_declaration
  name: (identifier) @entity.method.name @entity.method.signature.name
  (block)? @entity.method.body) @entity.method

(property_declaration
  type: (_) @entity.field.type @entity.field.signature.type
  name: (identifier) @entity.field.name @entity.field.signature.name
  accessors: (accessor_list)? @entity.field.body) @entity.field

(event_declaration
  name: (identifier) @entity.field.name @entity.field.signature.name) @entity.field

(namespace_declaration
  name: (identifier) @entity.package.name @entity.package.signature.name
  body: (declaration_list) @entity.package.body) @entity.package

(namespace_declaration
  name: (qualified_name) @entity.package.name @entity.package.signature.name
  body: (declaration_list) @entity.package.body) @entity.package

(file_scoped_namespace_declaration
  name: (identifier) @entity.package.name @entity.package.signature.name) @entity.package @entity.package.file_scoped

(file_scoped_namespace_declaration
  name: (qualified_name) @entity.package.name @entity.package.signature.name) @entity.package @entity.package.file_scoped

(delegate_declaration
  name: (identifier) @entity.function.name @entity.function.signature.name) @entity.function

(attribute
  name: (identifier) @entity.decorator.name) @entity.decorator

(field_declaration
  (variable_declaration
    type: (_) @entity.field.type
    (variable_declarator
      (identifier) @entity.field.name
      (_)? @entity.field.value))) @entity.field

(local_declaration_statement
  (variable_declaration
    type: (_) @entity.variable.type @entity.variable.signature.type
    (variable_declarator
      (identifier) @entity.variable.name
      (_)? @entity.variable.value))) @entity.variable

(local_declaration_statement
  (variable_declaration
    (variable_declarator
      (tuple_pattern
        (identifier) @entity.variable.name)
      (tuple_expression) @entity.variable.value))) @entity.variable

(foreach_statement
  left: (identifier) @entity.variable.name @entity.variable.signature.name
  right: (_) @entity.variable.source @entity.variable.signature.source) @entity.variable

(is_pattern_expression
  (declaration_pattern
    type: (_) @entity.variable.source
    name: (identifier) @entity.variable.name)) @entity.variable

(argument
  (declaration_expression
    type: (_) @entity.variable.type @entity.variable.signature.type
    name: (identifier) @entity.variable.name) @entity.variable) @entity.variable
"""


def call_query() -> str:
    return r"""
(invocation_expression
  function: (identifier) @call.function.name
  arguments: (argument_list) @call.function.arguments) @call.function

(invocation_expression
  function: (member_access_expression
    expression: (_) @call.method.object
    name: (identifier) @call.method.function)
  arguments: (argument_list) @call.method.arguments) @call.method

(invocation_expression
  function: (member_access_expression
    name: (identifier) @call.method.function)
  arguments: (argument_list) @call.method.arguments) @call.method

(invocation_expression
  function: (member_access_expression
    expression: (invocation_expression) @call.method.chained.from
    name: (identifier) @call.method.chained.to)
  arguments: (argument_list) @call.method.arguments) @call.method.chained

(invocation_expression
  function: (member_access_expression
    expression: (identifier) @call.method.static.object
    name: (identifier) @call.method.static.function)
  arguments: (argument_list) @call.method.arguments) @call.method.static

(object_creation_expression
  type: (identifier) @call.constructor.name) @call.constructor

(object_creation_expression
  type: (qualified_name) @call.constructor.name) @call.constructor

(invocation_expression
  function: (generic_name) @call.function.name
  arguments: (argument_list) @call.function.arguments) @call.function

(invocation_expression
  function: (member_access_expression
    expression: (_) @call.method.object
    name: (generic_name) @call.method.function)
  arguments: (argument_list) @call.method.arguments) @call.method

(invocation_expression
  function: (member_access_expression
    name: (generic_name) @call.method.function)
  arguments: (argument_list) @call.method.arguments) @call.method
"""


def dependency_query() -> str:
    return r"""
(using_directive) @dependency.using

(using_directive
  name: (identifier) @dependency.using.alias.name) @dependency.using.alias

(using_directive
  (qualified_name) @dependency.using.alias.path) @dependency.using.alias

(class_declaration
  (base_list
    (_) @dependency.extend.name)) @dependency.extend

(struct_declaration
  (base_list
    (_) @dependency.implement.name)) @dependency.implement

(record_declaration
  (base_list
    (_) @dependency.extend.name)) @dependency.extend

(interface_declaration
  (base_list
    (type) @dependency.extend.name)) @dependency.extend
"""


def comment_query() -> str:
    return r"""
(comment) @comment.line

((comment) @comment.doc
  (#match? @comment.doc "^///"))
"""
