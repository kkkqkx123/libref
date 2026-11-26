# llm — 模型传输层

## 职责

embeddings / rerank / chat 三条 HTTP 传输路径的统一封装：URL 拼装、认证、错误分类、重试与共享韧性状态（令牌桶限流 + 熔断）。传输叶子，经构造/注入方式被消费，不感知检索或生成业务。

## 关键文件

| 文件 | 职责 |
|---|---|
| `gateway.py` | `LlmGateway` 统一网关与韧性状态共享 |
| `resilience.py` | `RateLimiter`（令牌桶）、`CircuitBreaker`（熔断）、`Resilience` 组合 |
| `retry.py` | `RetryPolicy`、指数退避 + Retry-After 下限、`execute_with_retry` |
| `errors.py` | 错误分类（可重试性 / 是否网络失败 / Retry-After） |
| `embeddings.py` | `EmbeddingClient`：批量嵌入、L2 归一化、维度校验 |
| `rerank.py` | `RerankClient`：相关性打分 |
| `chat.py` | `ChatClient`：非流式补全 + SSE 流式 |
| `models.py` | 模型白名单与维度目录 |

## 功能清单

### 错误分类（errors.py）

`LlmError` 基类带三个判定：`is_retryable()`、`is_network_failure()`、`retry_after_ms()`。子类语义：

- `LlmProviderError(status)`：429 或 5xx 可重试，同时计入熔断失败窗口；其他 ≥400 不可重试。
- `LlmTransportError` / `LlmTimeoutError`：可重试且计入熔断。
- `LlmDecodeError`（响应/维度/数量不匹配）、`LlmConfigError`、`LlmInvalidRequestError`：不可重试、不进熔断。
- `LlmCircuitOpenError`：熔断打开占位。

### 共享韧性状态（resilience.py + gateway.py）

- `RateLimiter`：令牌桶——单调时钟流逝补桶（上限 burst），`acquire()` 无令牌时按差额 `sleep((1-tokens)/rate)`。
- `CircuitBreaker(failure_threshold=5, cooldown=30s)`：连续失败计数达阈值即打开；冷却满自动半开（清零计数、放行单次探测）；成功全量复位。
- `Resilience`：`before_send` 先问熔断（打开抛 `LlmCircuitOpenError`）再拿令牌；`after_failure` **只对 `is_network_failure()` 为真的异常计数**——429/5xx/传输/超时进熔断，配置与解码错误不进。
- **共享方式**：`LlmGateway.resilience_for(base)` 以 base URL 为键缓存——同端点的 embed/rerank/chat（即使模型不同）共享同一份限流与熔断状态；未配置 rate 时只共享熔断器。`breaker_kwargs`/`rate_per_sec` 构造时可注入。

### 三条传输路径的共同骨架

`_post` 同构：拼 URL + Bearer 头 + JSON body → 单次发送包在 `with_resilience` 内 → 最外层 `execute_with_retry` 驱动。单次内错误映射：`httpx.TimeoutException` → 超时、`HTTPError` → 传输、429/5xx → 可重试 ProviderError（解析 Retry-After 头）。

- **embeddings**：按 batch_size（默认 64）分批；解析后按 `index` 排序、拒重复索引、**每个向量 L2 归一化**、维度不符/数量不符抛 DecodeError。已知模型维度白名单（`BAAI/bge-m3: 1024`）配置期交叉校验。空输入零调用。
- **rerank**：文档先按字符截断（默认 500）再发送；结果数量必须等于文档数、`relevance_score`/`score` 双兼容、按分数降序返回 `(index, score)`。启用门：`rerank_enabled` + model + embedding key（base/key 复用 embedding 值）。
- **chat**：非流式 POST `/chat/completions`，解析 `choices[0].message`（含 tool_calls），temperature 固定 0.0；**流式走 SSE 解析器**（逐行取 `data:` delta，`[DONE]` 结束），**流式不重试**，但熔断簿记手动接在 before/after 上。启用门：`chat_enabled` + model + key（key/base 回退 embedding 值）。

### 重试策略（retry.py）

`execute_with_retry(policy, operation, is_retryable, retry_after_ms_of, ...)`：可重试异常且未超 `max_retries`（默认 3）→ 延迟 = `max(指数退避 base·2^(n-1), Retry-After 下限)` → 发 `RetryAttempt` 观测事件 → sleep 重试。无 policy 单次直跑。

## 对外接口

`LlmGateway`（`embed_fn`/`rerank_fn`/`chat_fn`/`chat_result_fn`/`chat_stream_fn`）、`build_embed_fn`/`build_chat_fn` 等独立组装入口、`EmbeddingClient`/`RerankClient`/`ChatClient`、`RetryPolicy`。

## 依赖与约束

传输叶子，仅依赖 httpx 与 pydantic；配置门控在各 `Config.from_settings`（key 缺失返回 None，调用方回退——嵌入缺失回退纯全文检索）。测试注入：传 transport 时每次调用开临时 `httpx.AsyncClient`（MockTransport）。凭据只经 settings 环境变量，不落存储与日志。
