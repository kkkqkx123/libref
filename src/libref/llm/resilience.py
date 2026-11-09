"""Transport protection, blueprint llm-suite-client/resilience; rate limiting and breaking are off by default and take effect once wired by the gateway."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from libref.llm.errors import LlmCircuitOpenError, LlmConfigError, LlmError


class RateLimiter:
    """Token-bucket rate limiting; passes within burst, waits for the shortfall otherwise."""

    def __init__(self, rate_per_sec: float, burst: int) -> None:
        if rate_per_sec <= 0:
            raise LlmConfigError("rate limiter rate must be positive")
        if burst < 1:
            raise LlmConfigError("rate limiter burst must be at least one")
        self._rate = rate_per_sec
        self._burst = float(burst)
        self._tokens = float(burst)
        self._updated = time.monotonic()

    async def acquire(self) -> None:
        """Return once a token is taken; when the bucket is empty, wait until one accrues."""
        while True:
            now = time.monotonic()
            elapsed = now - self._updated
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._updated = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            await asyncio.sleep((1.0 - self._tokens) / self._rate)


class CircuitBreaker:
    """Break on consecutive failures; success resets the count; after cooldown allow a single probe."""

    def __init__(self, failure_threshold: int = 5, cooldown: float = 30.0) -> None:
        if failure_threshold < 1:
            raise LlmConfigError("breaker threshold must be at least one")
        if cooldown < 0:
            raise LlmConfigError("breaker cooldown must not be negative")
        self._threshold = failure_threshold
        self._cooldown = cooldown
        self._failures = 0
        self._opened_at: float | None = None

    def allow(self) -> bool:
        """Whether sending is currently allowed; after cooldown automatically enter half-open probing."""
        if self._opened_at is None:
            return True
        if time.monotonic() - self._opened_at >= self._cooldown:
            self._opened_at = None
            self._failures = 0
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = time.monotonic()


@dataclass
class Resilience:
    """Shared protection bundle for one upstream; defaults disable all protection."""

    limiter: RateLimiter | None = None
    breaker: CircuitBreaker | None = None

    async def before_send(self) -> None:
        if self.breaker is not None and not self.breaker.allow():
            raise LlmCircuitOpenError()
        if self.limiter is not None:
            await self.limiter.acquire()

    def after_success(self) -> None:
        if self.breaker is not None:
            self.breaker.record_success()

    def after_failure(self, exc: BaseException) -> None:
        if (
            self.breaker is not None
            and isinstance(exc, LlmError)
            and exc.is_network_failure()
        ):
            self.breaker.record_failure()


async def with_resilience[T](
    resilience: Resilience | None, operation: Callable[[], Awaitable[T]]
) -> T:
    """Account protection bookkeeping around a single send; run directly with no protections configured."""
    if resilience is None:
        return await operation()
    await resilience.before_send()
    try:
        result = await operation()
    except LlmError as exc:
        resilience.after_failure(exc)
        raise
    resilience.after_success()
    return result
