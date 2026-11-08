"""抽取记录模型：与落库列一一对应的跨包边界结构。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Confidence = Literal["EXTRACTED", "INFERRED", "AMBIGUOUS"]


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
    bases: list[str] = Field(default_factory=list)
    parent: str | None = None
    depth: int = 0


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
    repo: str = ""
