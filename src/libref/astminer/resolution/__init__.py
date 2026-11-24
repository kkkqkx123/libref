"""Two-pass call resolution: declaration facts, then cross-file aliases."""

from __future__ import annotations

from libref.astminer.resolution.orchestration import resolve_calls, resolve_one

__all__ = ["resolve_calls", "resolve_one"]
