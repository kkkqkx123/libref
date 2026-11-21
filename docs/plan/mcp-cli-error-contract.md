# MCP 与 CLI 错误返回契约：现状审计与统一抽象

> **实施状态：已落地（代码 + 测试）**。第 1–4 章描述改造前实测现状，保留作为审计证据；第 5–9 章的方案已实现，落地清单与验证结果见第 12 章。第 11 章的 8 个开放点已按"建议"列全部拍板执行。

## 0. 定位

本文回答三个问题：参数出错时 MCP 实际返回什么、这套设计是否合理、是否需要统一抽象（CLI 同理）。审计对象是三个出口里"失败如何表达"这一条链路，不涉及业务逻辑与检索质量。

结论先行：

- MCP 目前有**两套互不相通的错误通路**，且都不是设计出来的，而是 SDK 行为与工具内手工校验各管一段的产物。参数类型/缺失/枚举错误走协议层 `isError=true` 加 pydantic 原生长文本；工具内校验走载荷层 `isError=false` 加 `error/message`。`docs/app/mcp.md:3` 声称"不抛协议层错误，失败以载荷内错误返回"，实测不成立。
- 更常见的失败是**静默成功**：未知参数被丢弃、`limit=true` 被当成 1、非法 `ecosystem` 返回 0 条。模型拿不到任何纠错信号，这类问题比报错更难恢复。
- CLI 侧相反：43 个命令里 18 个没有任何异常边界，同一个 `--limit 0` 在 MCP 是结构化拒绝、在 CLI `search` 是 traceback、在 CLI `api-insight` 是静默空结果。
- 需要统一抽象，但只需一层：一个错误值对象加一张闭集合码表，加每个出口一个适配器。避免为"错误"再造第二套业务模型。

## 1. 三条通路与实际载荷

### 1.1 MCP

| 通路 | 触发条件 | `isError` | 载荷 | 代码位置 |
|---|---|---|---|---|
| 协议层 schema 校验 | 类型错、缺必填、枚举错 | `true` | pydantic 原生多行文本，无结构化字段 | SDK `tools/base.py:151-156` → `mcpserver/server.py:456` |
| 工具内手工校验 | 空串、数值范围、跨字段依赖 | `false` | `{"error": "invalid", "message": ...}` | `mcp_server/server.py:36-45`、`61-70` |
| 服务层异常 | `ValueError`/`RegistryError`/`IndexRequestError` | `false` | `{"error": code, "message": ...}` | `mcp_server/server.py:52-58`、`260-263` |
| 无通路 | 未知参数、宽松类型转换、未校验的枚举 | `false` | 正常成功载荷 | `func_metadata.py:270-272`、`store/retriever.py:58-60` |

文中不带 `src/` 前缀的 SDK 路径均指 `.venv` 内 `mcp==2.3.0`（如 `mcp/server/mcpserver/tools/base.py`）。

实测原始报文（`libref serve-mcp` + 裸 JSON-RPC，protocolVersion 协商为 `2025-11-25`）：

```text
search {"query":"torch","limit":"abc"}   → isError=true
  "Error executing tool search: 1 validation error for searchArguments
   limit
     Input should be a valid integer, unable to parse string as an integer
     [type=int_parsing, input_value='abc', input_type=str]
       For further information visit https://errors.pydantic.dev/2.14/v/int_parsing"

search {"query":"torch","limit":0}       → isError=false
  {"error": "invalid", "message": "limit must be between 1 and 100"}

search {"query":"torch","unknown_field":1} → isError=false，正常结果，未知键无提示
```

同一份代码里，`limit=0` 与 `limit="abc"` 走了两条通路，`isError` 相反，客户端无法用统一规则判断"这次调用失败了"。

### 1.2 CLI 与三出口对照

`cli/__init__.py:12-20` 只捕获 `ConfigError`；`argparse` 自身错误输出 usage 并 `exit 2`；其余异常直达 `main` 之外。

实测：

| 输入 | 出口 | 结果 | 退出码 |
|---|---|---|---|
| `--limit 0` | MCP `search` | `{"error":"invalid","message":"limit must be between 1 and 100"}` | — |
| `--limit 0` | CLI `search` | `ValueError` traceback（`store/retriever.py:484` 直穿） | 1 |
| `--limit 0` | CLI `api-insight` | 空输出，无任何提示 | 0 |
| `--limit 0` | Web `GET /search` | `422 {"detail":[{"loc":["query","limit"],"msg":"Input should be greater than or equal to 1","type":"greater_than_equal"}]}`（`web/api.py:78` 的 `ge=1, le=100`） | — |
| `--limit abc` | CLI `search` | `usage: ...` + `argument --limit: invalid int value: 'abc'` | 2 |
| `--limit abc` | MCP `search` | pydantic 文本，`isError=true` | — |

同一个参数、同一个语义，四个出口四种行为。上限同样不齐：同名 `limit` 在 MCP 内就有 4 种取值（`answer` 50、`search` 100、`job_show` 200、insight 类 500，`server.py:366,243,960,612`），Web 另写一套（`/search` 100、`/pages` 200，`web/api.py:78`、`web/app.py:612`），服务层只判"正数"（`store/retriever.py:484`）。

### 1.3 信封由三层各自生产

`services/infra/jobs.py:346,348` 直接返回 `{"error": "not_found", "message": ...}`；MCP 在 `server.py:36-45` 自己拼同一形状；Web 在 `web/errors.py:51-64` 拼 `{"error","detail","request_id"}`。三处独立维护同一契约。副作用是同名字段语义冲突：`job_show` 的顶层 `error` 是错误码，`jobs[].error` 是任务失败文本（`jobs.py:339,353`）。

## 2. 参数错误矩阵（实测）

| 场景 | 是否被拦下 | 返回 | 对模型的指导性 |
|---|---|---|---|
| 缺必填 `query` | 是 | pydantic 文本 | 中（字段名有，格式噪声大） |
| 类型错 `limit="abc"` | 是 | pydantic 文本 | 中 |
| 枚举错 `view="bogus"` | 是 | pydantic 文本（列出合法值） | 好（唯一带候选集的一类） |
| 未知参数 `top_k` | **否** | 正常结果 | 无 |
| 宽松转换 `limit="5"` | **否** | 按 5 执行 | 无（可接受） |
| 宽松转换 `limit=true` | **否** | 按 1 执行 | 无（危险：语义被改） |
| 范围错 `limit=0` | 是 | `error/message`，`isError=false` | 好 |
| 跨字段 `mode=window` 缺 `sort_order` | 是 | `error/message` | 好 |
| 未校验枚举 `ecosystem="pytorch"` | **否** | `count: 0` | 无（与"确实无结果"不可区分） |

`limit=true` 被接受的原因：SDK 的 `ArgModelBase` 取 pydantic 默认 `extra="ignore"` 与宽松模式（`func_metadata.py:270-272`、`358-362`），布尔转整数不报错。`ecosystem` 无校验的原因：`discovery.py:180-181` 校验了，`store/retriever.py:58-60` 只把它当 SQL 过滤条件。

## 3. 现有设计是否合理

站得住的部分：

- 失败**不用异常穿透协议**这一取向是对的：`not_found`、`answerable=false`、降级原因是业务结果，不是异常。
- `error/message` 两字段信封的方向正确，且 `resolve` 已经做了全套示范：`server.py:1007-1021` 返回 `hint` 加 `next_actions`（含 `mcp_tool`、`cli`、`arguments`、`purpose`），这是全项目唯一能直接驱动模型下一步动作的错误形状。`_with_next`（`1075-1082`）把 `next_tools` 挂在索引请求工具上，同一个思路。
- CLI 已有一处结构化出口：`request_commands.py:23-33` 的 `code: message`。

站不住的部分，按严重度排列：

1. **失败信号分裂**。参数错误 `isError=true`、业务错误 `isError=false`，且前者文本只有模型能读、后者带结构化字段。客户端按 `isError` 分支的逻辑对一半错误失效；`_otel.py:63-67` 这类按 `is_error` 埋点的观测也会漏掉业务错误。
2. **指导性缺口**。`invalid` 只给一句话，不给 `field`、不给合法值、不给下一步。27 个工具里只有 `resolve` 一个带 `next_actions`。对比 `resolve` 的 "registry unreachable → 稍后重试或沿用缓存"，`search` 的 "limit must be between 1 and 100" 已经是好的，`_failed(str(exc))`（21 处）直接把内部异常原文抛给模型。
3. **信息噪声**。三条实测样本 225–280 字符，其中首行前缀 67–77、URL 尾巴 76–82、`[type=..., input_type=...]` 约 20，真正有用的只有"字段名 + 期望约束"，约占 35%。
4. **静默成功**。未知参数、`limit=true`、非法 `ecosystem` 都不报错。模型据此认为调用成功并继续推理，是最坏的一类。
5. **校验重复三层且已有死代码**。签名 `Literal` 已保证取值，工具内又判一次：`server.py:407`（mode）、`483`、`485`、`661`、`708`、`763`、`810`、`854`、`916`、`991` 共 10 处不可达（实测 `read_doc(mode="bogus")` 由 pydantic 拦下，返回 `literal_error`）。`_check_limit` 的 `isinstance` 分支同样不可达（`61-70`）。范围校验在 MCP、Web、服务层各写一遍，上限已经漂移。
6. **码表三套**。MCP 用 `invalid/failed/conflict/unavailable` 加 `RegistryError.kind`（`not_found/retryable/no_docs`）加 `IndexRequestError.code`；Web 用 19 码闭集合（`web/api.py:14-33`）；两者靠 `web/errors.py:19-23` 的别名表缝起来（`invalid→invalid_argument`、`conflict→index_request_conflict`）。别名表本身就是漂移的证据。
7. **CLI 无统一边界**。18/43 命令无 `except`；错误文案有 `invalid:`（18 处）、`failed:`（2）、`error:`（1）、`remote failed`（6）四套；`invalid` 有的是退出码 2（`docs_commands.py:141-143`），有的直穿 traceback。
8. **远端丢失服务端错误码**。`remote.py:41-47` 先 `raise_for_status()` 再取 body，Web 精心构造的 `{"error","detail","request_id"}` 在 CLI 侧被压成 `remote failed: Client error '400 Bad Request' for url ...`。

结论：方向对，实现缺一层。缺的不是再加分支，而是"错误的唯一表示 + 每个出口一个适配器"。

## 4. 精简性与信息量实测

| 样本 | 总字符 | 无用部分 | 有用信息 |
|---|---|---|---|
| 类型错 | 249 | 前缀 75 + URL 行 78 + 类型标签 ≈ 20 | `only_files` 需为列表 |
| 缺必填 | 225 | 前缀 67 + URL 行 76 + 类型标签 ≈ 18 | 缺 `query` |
| 枚举错 | 280 | 前缀 77 + URL 行 82 + 类型标签 ≈ 20 | `view` 取四值之一 |

另外一套与"精简"相关的机制在成功路径上：结果同时出现在 `content`（`indent=2` 美化，`func_metadata.py:653`）与 `structuredContent`（紧凑），8 字段载荷实测 255 字节承载 113 字节信息，**冗余 2.26×**，其中 1.26× 来自缩进。

目标形状（紧凑 JSON，单行）与现状对照：

| 场景 | 现状 | 目标 | 字符 |
|---|---|---|---|
| 类型错 | 249 字符 pydantic 文本 | `{"error":"invalid_argument","message":"only_files must be an array of strings","field":"only_files"}` | 100 |
| 缺必填 | 225 字符 | `{"error":"invalid_argument","message":"query is required","field":"query"}` | 74 |
| 枚举错 | 280 字符 | `{"error":"invalid_argument","message":"view must be one of: usage, related, args, common","field":"view"}` | 105 |
| 未知参数 | 无（静默成功） | `{"error":"invalid_argument","message":"unknown parameter: top_k","field":"top_k","hint":"accepted: query, library, version, ecosystem, limit, preset, suggest, include_prerelease"}` | 179 |

未知参数这一条因为要列出合法键，必然超出常规预算；其余三类可稳定压在 110 字符以内。

## 5. 对 LLM 的六条硬要求

1. 一次调用只有一个"失败"信号，`isError` 与载荷不自相矛盾。
2. 每条错误必须含：错在哪个字段、期望什么、可选值或范围。三者缺一即不可自纠。
3. 消息是单句陈述，不含类名、栈、URL、类型标签、入参回显。
4. 参数错必须 `isError=true`，因为是调用方的错，需要显式纠错信号；业务结果（未命中、降级）保持 `isError=false`。
5. 能给出下一步就给，形状与 `next_actions` 对齐，不新造字段。
6. 单条错误预算：常规 ≤ 160 字符，未知参数（需列合法键）≤ 200；未知参数必须报错，不允许静默丢弃。

## 6. 统一抽象

一个值对象，一张闭集合码表，三个适配器。位置放 `src/libref/errors.py`（被 `services/` 与三个出口共用，`db` 不依赖它，符合 `AGENTS.md` 的一向依赖）。

形状示意（非完整实现）：

```python
# 唯一闭集合：搬 web/api.py:14-33 的 19 码，前端不感知变化
ErrorCode = Literal["unauthorized", "forbidden", "missing_scope", ...,
                    "invalid_argument", "not_found", "unavailable", ...]

# MCP / CLI 允许输出的粗集（⊂ ErrorCode），变体信息走 field
CoarseCode = Literal["invalid_argument", "not_found", "unauthorized",
                     "forbidden", "unavailable", "failed",
                     "quota_exceeded"]

class LibrefError(Exception):
    code: ErrorCode
    message: str        # 单句：字段 + 期望
    field: str | None   # 出错参数名
    hint: str | None    # 一句话补救
    next_actions: list[dict]  # 复用 resolver.next_actions_for 的形状

class ErrorPayload(BaseModel):   # 唯一线上形状
    error: ErrorCode
    message: str
    field: str | None = None
    hint: str | None = None
    next_actions: list[dict] | None = None
```

要点：

- 码表取现有 Web 19 码为闭集合（前端已依赖）；MCP/CLI 只能从中取 `CoarseCode` 那 7 个。`invalid→invalid_argument`、`failed→unavailable`、`conflict→`按来源取 `review_conflict` 或 `index_request_conflict`、`retryable/no_docs` 归入 `unavailable`/`not_found`，映射固定后 `web/errors.py:19-23` 的别名表（含 `conflict→index_request_conflict` 的单值近似）随之删除。
- 变体信息进 `field` 而非新码：`invalid_limit`/`invalid_offset`/`invalid_ecosystem` 在 MCP 侧统一为 `invalid_argument` + `field`。
- `RegistryError.kind` 收敛：`retryable→unavailable`（带 `hint`）、`no_docs→not_found`、`not_found→not_found`。
- 信封只在**出口适配器**生成；`jobs.py:346,348` 改为 `raise LibrefError("not_found", ...)`，服务层不再返回信封，同时解决 `job_show` 里 `error` 字段的两义。

## 7. MCP 侧改造

改造点集中在一处：请求级 middleware（SDK 支持，`mcp.middleware.append(...)`，`server/context.py:146-183`）。

1. **前置拦截（补上第 4 类通路）**
   - 取 `ctx.params["arguments"]` 的键，与工具的 `inputSchema.properties` 求差集；非空即返回 `invalid_argument` 加 `message="unknown parameter: top_k"`、`hint="accepted: query, limit, preset, ..."`。这是目前唯一能拦住"拼错参数名"的位置，SDK 层 `extra="ignore"` 不会报错。
   - 同一处按 schema 做一次严格预校验：`boolean` 与 `integer` 严格区分，`limit=true` 从"按 1 执行"变为报错；失败即用关键词映射成单句（`required`/`type`/`enum`/`minimum`/`maximum`），不再让 pydantic 文本出厂。校验器可用 `jsonschema` 4.26（随 mcp 传入，尚未在 `pyproject.toml` 显式声明，需补 `uv add`）或直接用 pydantic 二次校验，二者都不改 SDK。
   - 这样 SDK 自身的校验退化为兜底，理论上不再触发；真触发时统一映射为 `failed` 并记日志，避免把 `1 validation error for xxxArguments` 漏给模型。

2. **后置统一（收口 `isError`）**
   - 工具内的 `_invalid`/`_not_found`/`_failed`（`server.py:36-45`，86 + 2 + 21 处调用）改为 `raise LibrefError(...)`；middleware 捕获后统一转 `CallToolResult(isError=True, content=[紧凑 JSON], structuredContent=信封)`。
   - 判据按码分流：`invalid_argument`/`unauthorized`/`forbidden`/`quota_exceeded`/`failed`/`unavailable` → `isError=true`；`not_found` 与检索类结果（`answerable=false`、`degraded`、`reasons`）→ 保持 `isError=false` 的正常载荷。

3. **顺带解决的两件事**
   - `outputSchema` 可以从 `{"type":"object","additionalProperties":true}`（27/27 工具当前都是这个，等于没声明）收紧为成功形状。前提是 2 的分流：客户端只在非 `isError` 时校验结构化输出（`client/session.py:1101`、`func_metadata.py:205`），业务错误仍走 `isError=false` 时会直接校验失败。
   - 清掉 10 处不可达的 `Literal` 重复校验与 `_check_limit` 的 `isinstance` 分支；把范围常量收到一处（与 Web 共用），消除同名 `limit` 4 种上限的漂移。

4. **精简**：错误载荷只出 `content` 里的紧凑 JSON（不填 `structuredContent`，结构化输出对 `isError=true` 无定义）；成功结果保留 `structuredContent`，`content` 改为紧凑 JSON，省掉 1.26× 缩进开销。

## 8. CLI 侧改造

1. `cli/__init__.py` 的 `main` 增加统一边界：`LibrefError` → 渲染信封；`ValueError` → 折算 `invalid_argument`；其余异常保留 traceback（真崩溃仍要可诊断）。18 个无边界命令自动获得一致性，`cmd_search`、`cmd_mine`、`cmd_probe_docs` 的 traceback 随之消失。
2. 退出码固定映射，与 `docs/app/cli.md:5` 对齐：`invalid_argument`/用法 → 2；`not_found`/`unavailable`/`failed` → 1；`unauthorized`/`forbidden` → 2（是否单列 3 见开放点）。`cmd_link_stats` 这类把领域错误算作 2 的散点收敛到映射表。
3. `_config_parent` 增加全局 `--json`：错误时 stdout 出 `ErrorPayload`、stderr 出人类可读行。当前仅 `role-audit`/`role-split` 有局部 `--json`（`parsers.py:976,1005`），agent 只能解析自由文本。
4. `remote.call_json` 先读 body 再判状态：把服务端的 `{"error","detail","request_id"}` 还原成 `LibrefError`，保留 `request_id` 供排查。
5. 删除 `invalid:`/`failed:`/`error:`/`remote failed` 四套文案，统一为 `code: message` 加 `hint` 行。

## 9. Web 侧对齐

改动最小：`web/errors.py:19-23` 的 `SERVICE_CODE_ALIASES` 删除（服务层已按闭集合码抛错），`web/api.py:14-33` 的 `KnownErrorCode` 改为从 `libref/errors.py` 导入，`error_body` 保留 `request_id`/`resource`/`reset_at` 三个 Web 特有字段。三个出口共用一张码表后，"同一输入在三出口产出同一 code"才可测。

## 10. 验证口径

- **报文快照**：每个工具 × 四类错误（缺必填 / 类型错 / 枚举错 / 未知参数），钉住原始 JSON-RPC 载荷。这是本次审计用的方法，可直接固化成 `tests/integration/test_mcp_error_contract.py`。
- **字符预算**：单条错误信封 ≤ 160 字符（现状 225–280）；成功载荷 `content` 与 `structuredContent` 信息量比 ≤ 1.1（现状 2.26）。
- **一致性表驱动**：`limit=0`、`ecosystem="pytorch"`、`query=""` 三个输入，在 MCP / CLI / Web 三出口断言同一 `error` 码。
- **负向用例**：`limit=true`、`limit=5.7`、未知参数三个用例由"静默成功"改为断言失败。
- **业务结果不回归**：`not_found`、`answerable=false`、降级原因仍走 `isError=false`，`read_doc` 未命中不得变成 `isError=true`。

## 11. 待拍板开放点

| 编号 | 问题 | 选项 | 建议 |
|---|---|---|---|
| 1 | 业务错误（`not_found`）是否也置 `isError=true` | 全失败统一 true / 按码分流 | 分流：参数与权限 true，业务结果 false。否则 `read_doc` 未命中会被客户端当崩溃 |
| 2 | 码表以 Web 19 码为唯一闭集合，还是另立一套 8 码独立体系 | 复用 Web（粗集作子集） / 独立新体系 | 复用 Web：前端已依赖，且能直接测"三出口同码"；独立体系会让 `SERVICE_CODE_ALIASES` 以另一种形式复活 |
| 3 | 未知参数是否一律报错 | 报错 / 保持忽略 | 报错。模型拼错参数名是最常见错误，静默丢弃无法自纠 |
| 4 | 严格 schema 预校验会否误伤真实客户端 | 严格 / 保留宽松转换（`"5"`→5） | 严格区分 `boolean`/`integer`，但保留字符串数字转换（现网客户端确有此习惯，`func_metadata.py:228-250` 专门做了 `pre_parse_json`） |
| 5 | `next_actions` 是否进所有错误 | 全部 / 仅参数错与注册表错 | 全部参数错；业务错按需，避免载荷膨胀 |
| 6 | CLI `unauthorized`/`forbidden` 单独退出码 3 | 复用 2 / 新增 3 | 复用 2，先不加码值，脚本兼容优先 |
| 7 | 是否需要 `retryable` 布尔字段 | 需要 / 用码表达 | 用码表达（`unavailable` 即可重试），不加字段，控制载荷体积 |
| 8 | 成功载荷的 `content` 是否改紧凑 JSON | 改 / 保持美观 | 改。缩进省 1.26×，但可读性下降，需确认是否有客户端依赖缩进 |

## 12. 实施记录（已落地）

### 12.1 新增文件

| 文件 | 职责 |
|---|---|
| `src/libref/errors.py` | 唯一错误值对象 `LibrefError`、闭集合 19 码表、`normalize()` 分类、`coarse()` 粗化、`exit_code_for()` 退出码映射、`to_json()` 紧凑载荷 |
| `src/libref/limits.py` | 共享范围常量与 `require_limit()`，MCP / mining 服务 / Web 统一取值域，消除 50/100/200/500 漂移 |
| `src/libref/mcp_server/contract.py` | `ToolFailure`（`LibrefError` + SDK `ToolError` 双继承）、请求级 `ArgumentContract` 中间件：严格 schema 预校验、字符串数字宽松保留、`isError` 归一化、成功载荷 `content` 紧凑化 |
| `tests/unit/test_errors.py` | 码表闭集合、载荷紧凑、分类、退出码单测 |
| `tests/integration/test_mcp_error_contract.py` | 7 类调用方错误的表驱动协议级契约（走真实 stdio 二进制） |

### 12.2 改动文件

- `mcp_server/server.py`：删除 10 处不可达 `Literal`/类型重复校验；87 处手工错误载荷收敛为 `ToolFailure`；范围校验改用 `require_limit`；`build_mcp_server` 末尾安装契约中间件；所有工具经统一 `_wrap` 边界，把服务层抛出的 `LibrefError` 转成 SDK 可识别的 `ToolFailure`（否则被当崩溃）。
- `cli/`：新增全局 `--json` 开关与 `main()` 单一异常边界；删除 `invalid:`/`failed:`/`error:`/`remote failed`/`discovery failed` 五套文案与 `format_registry_error`；43 个命令的错误统一由 `render_error`/`report_failure` 渲染。
- `cli/remote.py`：`call_json` 先读 body 再判状态，还原服务端 `{"error","detail","request_id"}` 为 `LibrefError` 并保留 `request_id`。
- `services/infra/jobs.py`：`JobManager.show` 未命中由返回错误字典改为抛 `LibrefError("not_found")`；MCP / Web / CLI 三处嗅探 `"error" in out` 的旧逻辑同步移除。
- `services/code/mining_read.py`：5 个 insight 方法接入 `require_limit`，与 MCP 共用 `INSIGHT_ROWS`/`PER_FILE_ROWS`。
- `web/api.py`、`web/errors.py`：`KnownErrorCode`/`ALL_CODES` 改为从 `libref.errors` 导入；删除 `SERVICE_CODE_ALIASES`，改由 `libref.errors.coarse()` 收口。
- `pyproject.toml` / `uv.lock`：新增直接依赖 `jsonschema`（预校验）与 dev 依赖 `types-jsonschema`（类型检查），保持原阿里云源与 `revision=5`。
- 测试同步：`test_mcp_index_requests`、`test_registry_providers`、`test_mcp_tool_outputs`、`render_mcp_outputs.py` 适配新失败语义（失败由异常边界表达，不再从成功载荷里嗅探）。

### 12.3 开放点拍板结果

第 11 章 8 个开放点全部按"建议"列落地：分流 `isError`（参数/权限 true、业务结果 false）、复用 Web 19 码粗集、未知参数报错、严格类型但保留字符串数字、参数错带 `next_actions`、CLI 权限复用退出码 2、不加 `retryable` 字段、成功载荷改紧凑 JSON。

### 12.4 验证结果

- `pytest -q`：**825 passed, 59 skipped**（含新增契约测试）。
- `ruff check` / `ruff format --check` / `mypy src`：全部通过（173 个源文件）。
- 协议级复测（`audit-probes/probe_contract.py`）：枚举错 105 字符、值域错 88、空串 71、未知参数 179，全部 `isError=true` 且为单条紧凑 JSON；字符串数字 `"5"` 仍正常返回；`job_show` 未命中为 `isError=false` 业务结果。达标：错误载荷 ≤ 160（未知参数 ≤ 200）、成功载荷信息量比约 1.0（原 2.26）。
- 遗留：`libref/errors.py` 中 `coarse()` 对 Web 私有变体码的收口依赖调用方先传规范码；若后续新增错误码，需在闭集合表中登记，否则 `LibrefError` 构造时直接 `ValueError`（已有单测覆盖）。
