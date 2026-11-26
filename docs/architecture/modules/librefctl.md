# librefctl — 运行中服务器的 HTTP 客户端

## 职责

独立的 uv workspace 成员（与主包 libref 完全解耦，不 import libref）：面向 skills、脚本与外部 AI 助手的 HTTP-only 客户端，按 web 路由契约调用运行中的 libref 服务器。与本地 CLI（`libref` 命令）的分工——本机路径索引用 `libref local index-repo`，远程/队列式操作用 `librefctl`。

## 关键文件

| 文件 | 职责 |
|---|---|
| `commands.py` | 声明式命令表（`Param`/`Command` frozen dataclass + `COMMANDS` 元组） |
| `app.py` | argparse 树生成、参数分派、等待轮询、统一 JSON 输出 |
| `transport.py` | httpx 客户端、Bearer 认证、三种失败形态的解析 |
| `output.py` | `success`/`failure`/`emit` 统一输出信封 |

## 功能清单

### 声明式命令表（commands.py）

约 40 条命令，每条对应一个 web 端点：`Param`（name/flags/location/value_type/choices/action/…，location 分 path/query/body/local 四组）+ `Command`（空格子命令路径、method、带 `{占位符}` 的 path、`body_wrapper`（如 mine → `{kind: mine_patterns, params:{...}}`）、`wait` 型等待终态）。覆盖：search/answer/read/catalog、resolve 与三类搜索、六类 insight、link-stats/repo-deps、doc-probe/repo-probe、ingest-docs、mine/induce-labels、index-request 全生命周期（submit/list/show/approve/reject/cancel/wait）、job 三态、review 四动作、feedback、admin 身份管理。

### 解析与调度（app.py）

- `_JsonArgumentParser`：argparse 用法错误也输出统一 JSON 错误信封（退出码 2）。
- 全局参数：`--base-url`（默认 `LIBREF_BASE_URL` 或 http://127.0.0.1:8001）、`--api-key`（`LIBREF_CLIENT_API_KEY`）、`--request-timeout`、`--pretty`。
- `--target-prefix/--target-name` 合成 `target_filter` 对象；路径占位符经 `quote(..., safe="")` 转义。
- `wait` 型命令每秒轮询直到终态（request: done/failed/rejected/cancelled；job: done/failed），超时抛 `unavailable`，meta 带 waited/status/attempts。

### 传输与输出（transport.py / output.py）

- httpx `Client` 带 `Authorization: Bearer`；非 2xx 解析三种失败形态：FastAPI 校验错误（detail 列表 → `invalid_argument` + field）、稳定错误信封（`error` + `detail` + `request_id`）、无 JSON 的裸状态码（`unavailable`）。
- 输出信封：`{"ok": true, "data": ..., "meta": {...}}` / `{"ok": false, "error": {code, message, field?, request_id?}}`；退出码两档——usage 类错误码（invalid_argument/unauthorized/forbidden 等 10 个）→ 2，其余 → 1。

## 对外接口

`python -m librefctl`（`__main__.py`）单命令行入口；运行时配置只经环境变量（`LIBREF_BASE_URL`、`LIBREF_CLIENT_API_KEY`、`LIBREF_CLIENT_TIMEOUT`）。

## 依赖与约束

仅依赖 httpx，不依赖 libref 主包；与 web 路由的契约由 `tests/unit/test_librefctl_*` 验证。enforced 模式下每请求带 Bearer key，dev 模式无需 key。
