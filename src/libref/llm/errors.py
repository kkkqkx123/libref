"""Transport error classification, blueprint llm-suite-common/provider_error; shared by the embedding, rerank, and chat paths."""

from __future__ import annotations


class LlmError(Exception):
    """Base transport error; subclasses declare their own retryability."""

    def is_retryable(self) -> bool:
        return False

    def is_network_failure(self) -> bool:
        return False

    def retry_after_ms(self) -> int | None:
        return None


class LlmConfigError(LlmError, ValueError):
    """Invalid local configuration; rejected before sending, not retried."""


class LlmInvalidRequestError(LlmError, ValueError):
    """Invalid request payload; rejected before sending, not retried."""


class LlmProviderError(LlmError, RuntimeError):
    """Remote returned a non-success status; rate limiting and server faults are retryable."""

    def __init__(
        self, status: int, message: str, retry_after_ms: int | None = None
    ) -> None:
        super().__init__(f"provider returned {status}: {message}")
        self.status = status
        self.message = message
        self._retry_after_ms = retry_after_ms

    def is_retryable(self) -> bool:
        return self.status == 429 or 500 <= self.status <= 599

    def is_network_failure(self) -> bool:
        return self.status == 429 or 500 <= self.status <= 599

    def retry_after_ms(self) -> int | None:
        return self._retry_after_ms


class LlmDecodeError(LlmError, RuntimeError):
    """Response could not be decoded, including dim/count mismatches; not retried."""


class LlmTransportError(LlmError, RuntimeError):
    """Network-layer failure; retryable and counted toward the failure window."""

    def is_retryable(self) -> bool:
        return True

    def is_network_failure(self) -> bool:
        return True


class LlmTimeoutError(LlmError, RuntimeError):
    """Call exceeded its deadline; retryable and counted toward the failure window."""

    def __init__(self, message: str = "provider request timed out") -> None:
        super().__init__(message)

    def is_retryable(self) -> bool:
        return True

    def is_network_failure(self) -> bool:
        return True


class LlmCircuitOpenError(LlmError, RuntimeError):
    """Circuit-open placeholder; never raised until the rate-limit/breaker instances land, never retried."""

    def __init__(self, message: str = "circuit breaker is open") -> None:
        super().__init__(message)
