"""重试策略，蓝本 llm-suite-common/retry；三路传输共用同一驱动。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime


@dataclass(frozen=True)
class RetryAttempt:
    """单次重试调度描述，供观察回调消费。"""

    attempt: int
    max_retries: int
    delay_ms: int
    reason: str


@dataclass(frozen=True)
class RetryPolicy:
    """最大次数加基线延迟加指数退避，等待提示作为延迟下限。"""

    max_retries: int = 3
    base_delay: float = 0.5
    exponential: bool = True

    def delay_for_attempt(self, attempt: int, floor_ms: int = 0) -> float:
        """第几次重试前等待多少秒，序号从一开始。"""
        if self.exponential:
            computed = self.base_delay * (2.0 ** max(0, attempt - 1))
        else:
            computed = self.base_delay
        return max(computed, floor_ms / 1000.0)


def parse_retry_after_ms(value: str | None) -> int | None:
    """解析等待提示：秒数或 HTTP 日期，不可解析时返回空。"""
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
    """按策略执行异步操作，策略为空时只跑一次。"""
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
