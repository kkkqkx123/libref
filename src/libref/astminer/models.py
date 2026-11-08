"""抽取记录模型：与落库列一一对应的跨包边界结构。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Confidence = Literal["EXTRACTED", "INFERRED", "AMBIGUOUS"]


class ApiDeclRecord(BaseModel):
    qualified_name: str
    kind: str = Field(pattern="^(class|function|method)$")
    file: str
    line_start: int
    line_end: int


class RawCall(BaseModel):
    raw_callee: str
    args: list[str] = Field(default_factory=list)
    caller_file: str
    caller: str | None = None
    line_start: int
    line_end: int


class CallInstanceRecord(BaseModel):
    callee: str
    raw_callee: str = ""
    args: list[str] = Field(default_factory=list)
    caller_file: str
    caller: str | None = None
    line_start: int
    line_end: int
    confidence: Confidence
    repo: str = ""
