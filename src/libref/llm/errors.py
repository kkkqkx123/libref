"""传输错误分类，蓝本 llm-suite-common/provider_error；编码重排问答三路共用。"""

from __future__ import annotations


class LlmError(Exception):
    """传输错误基类，子类各自声明是否可重试。"""

    def is_retryable(self) -> bool:
        return False

    def is_network_failure(self) -> bool:
        return False

    def retry_after_ms(self) -> int | None:
        return None


class LlmConfigError(LlmError, ValueError):
    """本地配置无效，发送前拒绝，不重试。"""


class LlmInvalidRequestError(LlmError, ValueError):
    """请求载荷无效，发送前拒绝，不重试。"""


class LlmProviderError(LlmError, RuntimeError):
    """远端返回非成功状态，限流与服务端故障可重试。"""

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
    """响应无法解码，含维度与数量失配，不重试。"""


class LlmTransportError(LlmError, RuntimeError):
    """网络层故障，可重试并计入故障窗口。"""

    def is_retryable(self) -> bool:
        return True

    def is_network_failure(self) -> bool:
        return True


class LlmTimeoutError(LlmError, RuntimeError):
    """调用超出时限，可重试并计入故障窗口。"""

    def __init__(self, message: str = "provider request timed out") -> None:
        super().__init__(message)

    def is_retryable(self) -> bool:
        return True

    def is_network_failure(self) -> bool:
        return True


class LlmCircuitOpenError(LlmError, RuntimeError):
    """熔断器开路占位，限流熔断实例落地前不抛，永不重试。"""

    def __init__(self, message: str = "circuit breaker is open") -> None:
        super().__init__(message)
