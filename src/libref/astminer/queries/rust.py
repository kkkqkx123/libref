"""Rust queries: entity, call, dependency, and comment patterns.

Subset of the validated code-context-engine scheme, renamed to the engine
capture contract: entity roots land on mapped kinds, calls use the shared
object/function/arguments suffixes, dependencies feed alias building plus
extend/implement bases, comments feed line/doc spans.
"""

from __future__ import annotations


def entity_query() -> str:
    return r"""
(struct_item
  name: (type_identifier) @entity.struct.name @entity.struct.signature.name
  type_parameters: (_)? @entity.struct.signature.type_params
  body: (_) @entity.struct.body) @entity.struct

(enum_item
  name: (type_identifier) @entity.enum.name @entity.enum.signature.name
  type_parameters: (_)? @entity.enum.signature.type_params
  body: (enum_variant_list) @entity.enum.body) @entity.enum

(enum_variant
  name: (identifier) @entity.enum_constant.name @entity.enum_constant.signature.name) @entity.enum_constant

(union_item
  name: (type_identifier) @entity.union.name @entity.union.signature.name
  type_parameters: (_)? @entity.union.signature.type_params
  body: (field_declaration_list) @entity.union.body) @entity.union

(trait_item
  name: (type_identifier) @entity.interface.name @entity.interface.signature.name
  type_parameters: (_)? @entity.interface.signature.type_params
  body: (declaration_list) @entity.interface.body) @entity.interface

(type_item
  name: (type_identifier) @entity.type.name @entity.type.signature.name) @entity.type

(function_item
  name: (identifier) @entity.function.name @entity.function.signature.name
  type_parameters: (_)? @entity.function.signature.type_params
  parameters: (parameters) @entity.function.params @entity.function.signature.params
  return_type: (_)? @entity.function.return_type @entity.function.signature.return_type
  body: (_) @entity.function.body) @entity.function

(function_signature_item
  name: (identifier) @entity.function.name @entity.function.signature.name
  type_parameters: (_)? @entity.function.signature.type_params
  parameters: (parameters) @entity.function.params @entity.function.signature.params
  return_type: (_)? @entity.function.return_type @entity.function.signature.return_type) @entity.function

(mod_item
  name: (identifier) @entity.module.name @entity.module.signature.name
  body: (declaration_list) @entity.module.body) @entity.module

(const_item
  name: (identifier) @entity.variable.name @entity.variable.signature.name) @entity.variable

(static_item
  name: (identifier) @entity.variable.name @entity.variable.signature.name) @entity.variable

(let_declaration
  pattern: (identifier) @entity.function.name @entity.function.signature.name
  value: (closure_expression
    parameters: (closure_parameters) @entity.function.params @entity.function.signature.params
    return_type: (_)? @entity.function.return_type @entity.function.signature.return_type)) @entity.function

(let_declaration
  pattern: (identifier) @entity.variable.name @entity.variable.signature.name) @entity.variable

(field_declaration
  name: (field_identifier) @entity.field.name
  type: (_) @entity.field.type) @entity.field

(impl_item
  type: (_) @entity.impl.type.name
  body: (declaration_list)? @entity.impl.body) @entity.impl
"""


def call_query() -> str:
    return r"""
(call_expression
  function: (identifier) @call.function.name
  arguments: (arguments)? @call.function.arguments) @call.function

(call_expression
  function: (field_expression
    value: (_) @call.method.object
    field: (field_identifier) @call.method.function)
  arguments: (arguments)? @call.method.arguments) @call.method

(call_expression
  function: (field_expression
    value: (call_expression) @call.method.chained.from
    field: (field_identifier) @call.method.chained.to)
  arguments: (arguments)? @call.method.arguments) @call.method.chained

(call_expression
  function: (scoped_identifier
    path: (_) @call.method.object
    name: (identifier) @call.method.function)
  arguments: (arguments)? @call.method.arguments) @call.method

(macro_invocation
  macro: (identifier) @call.function.name) @call.macro
"""


def dependency_query() -> str:
    return r"""
(use_declaration
  argument: [(scoped_identifier) (identifier)] @dependency.use.path) @dependency.use

(use_declaration
  argument: (use_as_clause
    path: (_) @dependency.use.alias.path
    alias: (identifier) @dependency.use.alias.name)) @dependency.use.alias

(use_declaration
  argument: (use_wildcard) @dependency.use.wildcard) @dependency.use.wildcard

(use_declaration
  argument: (scoped_use_list
    path: (_) @dependency.use.list.path
    list: (use_list) @dependency.use.list.items)) @dependency.use.list
"""


def comment_query() -> str:
    return r"""
(line_comment) @comment.line

(doc_comment) @comment.doc

(block_comment) @comment.doc
"""
