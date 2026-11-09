"""Retry policy, blueprint llm-suite-common/retry; all three transport paths share one driver."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime


@dataclass(frozen=True)
class RetryAttempt:
    """Description of a single retry scheduling, consumed by observation callbacks."""

    attempt: int
    max_retries: int
    delay_ms: int
    reason: str


@dataclass(frozen=True)
class RetryPolicy:
    """Max attempts plus base delay plus exponential backoff; Retry-After hints act as a delay floor."""

    max_retries: int = 3
    base_delay: float = 0.5
    exponential: bool = True

    def delay_for_attempt(self, attempt: int, floor_ms: int = 0) -> float:
        """Seconds to wait before the given retry; numbering starts at 1."""
        if self.exponential:
            computed = self.base_delay * (2.0 ** max(0, attempt - 1))
        else:
            computed = self.base_delay
        return max(computed, floor_ms / 1000.0)


def parse_retry_after_ms(value: str | None) -> int | None:
    """Parse a Retry-After hint: seconds or an HTTP date; return None if unparseable."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return int(float(text) * 1000)
    except ValueError:
        pass
    try:
        deadline = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if deadline is None:
        return None
    from datetime import datetime

    now = datetime.now(UTC)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return max(0, int((deadline - now).total_seconds() * 1000))


async def execute_with_retry[T](
    policy: RetryPolicy | None,
    operation: Callable[[], Awaitable[T]],
    *,
    is_retryable: Callable[[BaseException], bool],
    retry_after_ms_of: Callable[[BaseException], int | None] | None = None,
    on_retry: Callable[[RetryAttempt], None] | None = None,
    reason_of: Callable[[BaseException], str] | None = None,
) -> T:
    """Run the async operation per the policy; with no policy, run once."""
    if policy is None:
        return await operation()
    attempt = 0
    while True:
        try:
            return await operation()
        except Exception as exc:
            if not is_retryable(exc) or attempt >= policy.max_retries:
                raise
            attempt += 1
            floor = retry_after_ms_of(exc) if retry_after_ms_of is not None else None
            delay = policy.delay_for_attempt(attempt, floor or 0)
            if on_retry is not None:
                reason = reason_of(exc) if reason_of is not None else str(exc)
                on_retry(
                    RetryAttempt(attempt, policy.max_retries, int(delay * 1000), reason)
                )
            await asyncio.sleep(delay)
