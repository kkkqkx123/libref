"""Extraction record models: cross-package boundary structures matching storage columns one-to-one."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Confidence = Literal["EXTRACTED", "INFERRED", "AMBIGUOUS"]

TARGET_STATUS_RESOLVED = "resolved_target"
TARGET_STATUS_PREFIX = "prefix_candidate"
TARGET_STATUS_UNCONFIRMED = "version_unconfirmed"
TARGET_STATUS_EXTERNAL = "not_target"

ResolveKind = Literal[
    "alias-direct",
    "alias-external",
    "alias-suffix",
    "stdlib",
    "factory",
    "import-preferred",
    "inherited",
    "self-direct",
    "self-inherited",
    "receiver-type",
    "same-file",
    "suffix",
    "scope",
    "builtin",
    "param-shadowed",
    "unresolved",
]


class CodeSnippet(BaseModel):
    sample_id: str
    language: str
    code: str
    url: str = ""
    heading: str = ""
    ordinal: int = 0
    origin_kind: str = "markdown_fence"
    # Host location of the block, so call evidence points back to the page
    # or source file instead of only to a synthetic parse path.
    origin_location: str = ""
    origin_start_byte: int = 0
    origin_end_byte: int = 0
    origin_start_line: int = 0
    origin_end_line: int = 0
    import_context: str = ""


class ApiDeclRecord(BaseModel):
    qualified_name: str
    kind: str = Field(min_length=1)
    file: str
    line_start: int
    line_end: int
    start_byte: int = 0
    end_byte: int = 0
    signature: str = ""
    params: list[str] = Field(default_factory=list)
    param_types: list[str] = Field(default_factory=list)
    return_type: str = ""
    bases: list[str] = Field(default_factory=list)
    parent: str | None = None
    depth: int = 0
    has_doc: bool = False
    span_kind: str = "exact_entities"
    preproc_conditions: list[str] = Field(default_factory=list)
    macro_body_signature: str = ""
    macro_body_hash: str = ""


class RawCall(BaseModel):
    raw_callee: str
    args: list[str] = Field(default_factory=list)
    caller_file: str
    caller: str | None = None
    line_start: int
    line_end: int
    start_byte: int = 0
    end_byte: int = 0
    receiver: str | None = None
    func: str = ""
    argument_count: int = 0
    relation_kind: str = "direct"
    macro_generated: bool = False
    macro_host: str = ""
    macro_host_file: str = ""
    macro_definition_line: int = 0
    macro_nesting: int = 0
    macro_conditions: list[str] = Field(default_factory=list)
    macro_body_kind: str = ""
    macro_body_signature: str = ""


class CallInstanceRecord(BaseModel):
    callee: str
    raw_callee: str = ""
    args: list[str] = Field(default_factory=list)
    caller_file: str
    caller: str | None = None
    line_start: int
    line_end: int
    start_byte: int = 0
    end_byte: int = 0
    receiver: str | None = None
    func: str = ""
    argument_count: int = 0
    call_order: int = 0
    confidence: Confidence
    resolve_kind: str = ""
    repo: str = ""
    commit_sha: str = ""
    source_id: int | None = None
    sample_id: str = ""
    source_role: str = "implementation"
    origin_kind: str = "repo_file"
    in_target_api: bool = False
    target_status: str = TARGET_STATUS_RESOLVED
    relation_kind: str = "direct"
    macro_generated: bool = False
    macro_host: str = ""
    macro_host_file: str = ""
    macro_definition_line: int = 0
    macro_nesting: int = 0
    macro_conditions: list[str] = Field(default_factory=list)
    macro_body_kind: str = ""
    macro_body_signature: str = ""
