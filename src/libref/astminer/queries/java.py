"""Java 查询：实体、调用、依赖、注释四域的模式文本。"""

from __future__ import annotations


def entity_query() -> str:
    return r"""
(class_declaration
  name: (identifier) @entity.class.name @entity.class.signature.name
  type_parameters: (_)? @entity.class.signature.type_params
  superclass: (superclass
    (type_identifier) @entity.class.base @entity.class.signature.base)?
  body: (class_body) @entity.class.body) @entity.class

(interface_declaration
  name: (identifier) @entity.interface.name @entity.interface.signature.name
  type_parameters: (_)? @entity.interface.signature.type_params) @entity.interface

(enum_declaration
  name: (identifier) @entity.enum.name @entity.enum.signature.name) @entity.enum

(enum_constant
  name: (identifier) @entity.enum_constant.name @entity.enum_constant.signature.name) @entity.enum_constant

(record_declaration
  name: (identifier) @entity.record.name @entity.record.signature.name
  type_parameters: (_)? @entity.record.signature.type_params) @entity.record

(annotation_type_declaration
  name: (identifier) @entity.annotation.name @entity.annotation.signature.name) @entity.annotation

(method_declaration
  type_parameters: (_)? @entity.method.signature.type_params
  type: (_)? @entity.method.return_type @entity.method.signature.return_type
  name: (identifier) @entity.method.name @entity.method.signature.name
  parameters: (formal_parameters) @entity.method.params @entity.method.signature.params
  body: (block)? @entity.method.body) @entity.method

(constructor_declaration
  name: (identifier) @entity.constructor.name @entity.constructor.signature.name
  parameters: (formal_parameters) @entity.constructor.params @entity.constructor.signature.params
  body: (block)? @entity.constructor.body) @entity.constructor

(field_declaration
  declarator: (variable_declarator
    name: (identifier) @entity.field.name)) @entity.field

(local_variable_declaration
  type: (_) @entity.variable.type
  declarator: (variable_declarator
    name: (identifier) @entity.variable.name)) @entity.variable

(package_declaration
  (scoped_identifier) @entity.package.name) @entity.package
"""


def call_query() -> str:
    return r"""
(method_invocation
  name: (identifier) @call.function.name
  arguments: (argument_list)? @call.function.arguments) @call.function

(method_invocation
  object: (_) @call.method.object
  name: (identifier) @call.method.function
  arguments: (argument_list)? @call.method.arguments) @call.method

(method_invocation
  object: (method_invocation) @call.method.chained.from
  name: (identifier) @call.method.chained.to) @call.method.chained

(method_invocation
  object: (identifier) @call.method.static.object
  name: (identifier) @call.method.static.function) @call.method.static

(object_creation_expression
  type: (type_identifier) @call.constructor.name
  (argument_list)? @call.constructor.arguments) @call.constructor

(explicit_constructor_invocation
  (super) @call.constructor.super) @call.constructor.super

(method_reference) @call.reference

(method_invocation
  name: (identifier) @call.hof.method.name
  arguments: (argument_list
    (lambda_expression) @call.hof.callback)) @call.hof.lambda

(method_invocation
  name: (identifier) @call.hof.method.name
  arguments: (argument_list
    (method_reference) @call.hof.callback)) @call.hof.method_ref
"""


def dependency_query() -> str:
    return r"""
(import_declaration
  (scoped_identifier) @dependency.import.name) @dependency.import

(import_declaration
  "static" @dependency.import.static) @dependency.import.static

(import_declaration
  (asterisk) @dependency.import.wildcard) @dependency.import.wildcard

(superclass
  (type_identifier) @dependency.extend.name) @dependency.extend

(super_interfaces
  (type_list
    (type_identifier) @dependency.implement.name)) @dependency.implement

(requires_module_directive
  (scoped_identifier) @dependency.module.requires.name) @dependency.module.requires

(exports_module_directive
  (scoped_identifier) @dependency.module.exports.name) @dependency.module.exports

(opens_module_directive
  (scoped_identifier) @dependency.module.opens.name) @dependency.module.opens

(uses_module_directive
  (scoped_identifier) @dependency.module.uses.name) @dependency.module.uses

(provides_module_directive
  (scoped_identifier) @dependency.module.provides.name) @dependency.module.provides
"""


def comment_query() -> str:
    return r"""
(line_comment) @comment.line

(block_comment) @comment.doc
"""
