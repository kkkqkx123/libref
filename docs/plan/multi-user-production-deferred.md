# 多用户服务：本阶段不做的任务与替代方案（后续参考）

> 配套文档：`docs/plan/multi-user-production-deployment.md`（v2，4 角色与 11 项开放点已拍板）。
> 定位：记录**本阶段明确不做**的任务，以及**被已拍板结论取代的替代方案**，供后续阶段启动时直接复用，不留悬空问题。
> 事实基线同主方案（`6c8004b95f54136fbbcf6850c44c1d1c10e7d21b`）；所有现状结论附 `file:line`。按 `AGENTS.md:11`，不贴完整代码片段。
> 本文不产生本阶段的实施项；此处任一任务启动时，应新建独立方案并回填主方案 §11.5 的验证口径。

---

## 1. 本阶段不做的任务

| # | 任务 | 现状证据 | 本阶段不做的理由（拍板） | 后续启动条件 | 关联 |
|---|---|---|---|---|---|
| T1 | 后端存储抽象，支持远程存储后端（pg + es + qdrant） | `db/conn.py:10-23` 直接 `sqlite3.connect` + PRAGMA；`db/schema.py` 全为 SQLite DDL；`store/vec_store.py:22-27` 集合名按 db 路径派生 | **O3**：后续将进一步设计后端抽象，本阶段暂不考虑；提前抽象会与 `AGENTS.md:74-75` 的「db 唯一 SQL 底层」冲突 | 多租户数据量/并发写超出 SQLite 单机能力，或需要跨主机共享真源 | 主方案 §6.2 |
| T2 | 按主体的配额与计量口径（请求数 / LLM token / 存储容量 / 抓取页数） | 无请求级限流；`RateLimiter` 仅 LLM 客户端侧且默认未启用（`llm/gateway.py:42-50`、`llm/resilience.py:1`）；`config.py:197,201` 的配额是全局单值 | **O8**：暂时不实现，作为后续任务 | 出现多租户成本分摊或公平性诉求；需先定义计量维度与超额策略（拒绝 vs 降级，降级语义见 `web/app.py:122-125`） | 主方案 §7.4 |
| T3 | `uv.lock` 入库与 Python 3.14 交付验证 | `.gitignore:38-39` 明确排除 `uv.lock`；`pyproject.toml:9` 要求 `>=3.14`；`AGENTS.md:48` 却写 `uv sync from uv.lock`；`AGENTS.md:59` 要求升级时重验二进制依赖 | **O10**：该问题不管，属部署设计的一部分，与本阶段无关 | 进入镜像/CI 交付阶段 | 主方案 §0.1 |
| T4 | Dockerfile / compose / 反向代理 / TLS / CI 完整交付 | 仓库无这些文件；`README.md` 为 0 字节；`docs/ops/deployment.md` 仅 32 行 | **O10 同类**：本阶段只给 `deploy/` 示例配置（O11），不做完整交付 | 部署设计阶段 | 主方案 §10.5 |
| T5 | 多副本、水平扩展与滚动升级 | `uvicorn.run(...)` 未传 workers（`cli/ops_commands.py:317`）；无 drain，连接只在 `run()` 返回后关闭（:281,318） | **O6** 拍板同进程单应用；O10 不管部署编排 | 需要高可用或多机部署；届时 DB 认领（主方案 §8）已具备基础，但还需 session 共享与配置一致性 | 主方案 §8、§11.5 |
| T6 | 密码登录 / 会话 / 交互式身份 | 全库无 `session`/`user_id`；`config.py:254-256` 只有上游密钥与静态 `mcp_token` | **O2** 拍板自建用户表 + API Key；外部 AI 助手明确「无需人工登录」，人类主体也统一用 Key，避免引入会话状态 | 出现浏览器端人工登录诉求 | 主方案 §5.1 |
| T7 | 指标与链路追踪（Prometheus / tracing） | `observe.py:29-47` JSON 日志仅 4 字段；无访问日志中间件 | 属 T2 计量的附属能力；本阶段只做结构化日志与审计表（主方案 §10.3） | 进入可观测性专项 | 主方案 §10.3 |
| T8 | 按主体的文件资产配额核算 | `config.py:194-201` 的 `snapshot_dir`/`repos_dir`/配额为全局单值 | 属 T2；本阶段保留全局上限作为下限保护，慢查询闸门见主方案 §7.3 | 同 T2 | 主方案 §6.3 |
| T9 | 前端 `webui/` | 仓库不存在该目录，`docs/architecture/module-layout.md:142` 仍按目标态描述 | 范围外（主方案 §0.2） | 前端立项 | — |
| T10 | 检索质量调优与向量后端选型 | `services/infra/vectors.py` 双后端（simvec/qdrant）；`docs/architecture/tech-stack.md:13,19` | 范围外，与多用户改造正交 | 检索专项 | — |
| T11 | ruff 工具链版本固定（`ruff>=0.6` 加上界或 `uv.lock` 锁定）与全库格式统一重排 | `pyproject.toml:80` 声明 `ruff>=0.6` 无上界；`pyproject.toml:53-58` 为本轮新增的 `[tool.ruff]` 主配置；修复后 `ruff format --check src tests` 在 0.6.9 下仍有 12 个文件需重排、0.17.5 下 20 个 | 本轮只修 P0 语法并补 `tests/unit/test_syntax.py` 门禁（主方案 §1.12）；强行重排会产生与缺陷无关的大规模 diff，且目标环境 ruff 版本未知 | 与 T3/T4 的镜像/CI 交付阶段合并，一次性固定版本并重排，把 `ruff format --check` 纳入流水线 | 主方案 §1.12 |

---

## 2. 被取代的替代方案

### 2.1 隔离粒度

| 方案 | 对应结论 | 现状证据 | 本阶段为何不选 | 后续启用的代价 |
|---|---|---|---|---|
| **A1 多库物理隔离**（每租户独立 db 文件） | O1 选了共享库 + `tenant_id` | `config.py:51-53` 单 `db_path`；`store/vec_store.py:22-27` 集合名按 db 文件绝对路径哈希派生；`docs/ops/deployment.md:9-13` 已描述「不同库文件天然隔离」 | 实例数、连接数与运维成本随租户线性增长；与「同进程单应用」（O6）组合后收益更低 | 需把 `db_path` 改为按主体解析、连接池按租户路由、向量集合派生规则保留；主方案 §6 的 `tenant_id` 列可保留为冗余或删除 |
| **A1' 共享库但按 schema 分表** | O1 | `db/schema.py` 单套表名 | 迁移数量随租户增长，SQLite 下不划算 | — |

> A1 是**最贴近现有代码**的替代方案（几乎只需改 `db_path` 解析），若后续出现「租户数据必须物理隔离」的合规要求，应优先重新评估它，而不是直接跳到 PostgreSQL。

### 2.2 鉴权方案

| 方案 | 对应结论 | 现状证据 | 本阶段为何不选 | 后续启用的代价 |
|---|---|---|---|---|
| **A2 JWT / OIDC 对接身份系统** | O2 选了自建用户表 + API Key | 无用户表；`mcp_server/auth.py:36-48` 只有静态 token 比较 | 需引入 IdP、JWKS 轮换、audience 校验；外部 AI 助手是机器主体，无人工登录，OIDC 反而增加流程 | 新增 token verifier 中间件；`api_keys` 表可保留为本地凭据或退役 |
| **A3 MCP OAuth 2.1 授权**（PKCE、RFC 8707 resource、per-tool scopes、`WWW-Authenticate` 挑战） | O2 / O5 | `server.py` 无任何 auth 引用；`mcp>=2.3.0` 已支持 OAuth 资源服务器形态（见[官方授权说明](https://restfulapi.net/mcp-server-authorization/)与[主方案 §1.10](multi-user-production-deployment.md)） | 本阶段服务面向受控内网与自建主体，静态 API Key 足够；OAuth 属于「公开/多客户 MCP 服务器」层级 | 需实现或部署授权服务器、保护资源元数据端点、把 scope 映射到 tool 级拒绝；`ALL_MCP_TOOLS`（`server.py:57-80`）需按 scope 动态可见 |
| **A4 反向代理前置鉴权**（服务只信可信 header） | O2 | `web/app.py:69-75` 的 `_guard` 目前直接读 header；无可信代理边界 | 服务侧失去主体身份，无法做 `tenant_id` 归属与审计；必须额外防绕过 | 需要网络层保证「只能经代理访问」，并补 header 签名校验 |
| **A5 密码登录 / 会话** | O2 | 全库无 `session` | 与「外部 AI 助手无需人工登录」冲突；引入状态 | 同 T6 |

### 2.3 任务队列

| 方案 | 对应结论 | 现状证据 | 本阶段为何不选 | 后续启用的代价 |
|---|---|---|---|---|
| **A6 外部消息队列**（Redis / Celery / RQ） | O4 选了 DB 认领 | `services/infra/jobs.py:68-95` 进程内 `asyncio.Queue` + jobs 表 write-through；`docs/architecture/tech-stack.md:25` 明确「不引入 Celery/Redis，单机系统引入外部队列是过度设计」 | 新增外部依赖与运维面，且推翻既有架构决策 | 需在 `pyproject.toml` 加依赖、改写 `submit`/worker、把 `heartbeat_at`/`recover`（`jobs.py:173-175,262-290`）迁到队列语义；`docs/architecture/tech-stack.md:25` 必须同步改写 |

### 2.4 配置更新

| 方案 | 对应结论 | 现状证据 | 本阶段为何不选 | 后续启用的代价 |
|---|---|---|---|---|
| **A7 配置热更新 / 广播 + 监听者模式** | O9 选了「显式 API 刷新」 | `LibrefSettings` 冻结只读（`config.py:48`、`config.py:3`）；`AGENTS.md:161` 明确「有效快照构建后冻结」；`setup_logging`（`observe.py:54-96`）按 fingerprint 幂等 | 配置一旦长驻可变，所有持有 settings 的组件都要能响应变更，而 `web/app.py:61-66` 与 `server.py:109-121` 目前是闭包捕获，改造面大且难验证 | 需先落地主方案 §10.2 的 `AppContext`（组件可替换），再引入变更事件总线；三种配置入口（CLI / env / toml，`config.py:527-558`）都要统一广播，并同步 `AGENTS.md:161` 的表述 |
| **A7' 定期轮询重载** | O9 | 同上 | 与显式刷新重复，且存在「半新半旧」窗口 | — |

### 2.5 部署形态

| 方案 | 对应结论 | 现状证据 | 本阶段为何不选 | 后续启用的代价 |
|---|---|---|---|---|
| **A8 Web 与 MCP 独立部署单元 / 多 worker** | O6 选了同进程挂 2 个应用 | `cli/ops_commands.py:254-282` 与 :285-318 是两个独立命令、各自建连；`uvicorn.run` 未传 workers（:317） | 同进程共享 `AppContext` 与连接工厂最简单；独立副本会把 DB 认领、配置刷新、审计拆成两套一致性问题 | 需配置 `RequestStateSecurity(keys=[...])`（`mcp.server.mcpserver`）并让所有 worker 用相同 keys 与 server name（[streamable-http 参考](https://github.com/github/awesome-copilot/blob/36e95dfda72140a68f577d5c894a551607606bd3/skills/python-mcp-server-generator/references/streamable-http.md)）；`stateless_http=True` 可让任意副本应答；配置指纹（`config.py:573-576`）用于副本一致性核对 |
| **A9 继续用 `mcp.run("streamable-http")` 自启服务** | O5/O6 | `cli/ops_commands.py:276-278` | 2.3.0 的 `run()` 会自己起一个 web server，无法与 Web app 同进程组合；且不便挂统一中间件 | 需要时改为 `streamable_http_app()` + `Mount`，即主方案 §9.2 的方案本身 |

### 2.6 存储与版本

| 方案 | 对应结论 | 现状证据 | 本阶段为何不选 | 后续启用的代价 |
|---|---|---|---|---|
| **A10 迁 PostgreSQL / 通用后端抽象** | O3 | `db/conn.py:16-23` 全为 SQLite PRAGMA；`db/schema.py` 全为 SQLite DDL 与 FTS5 | 后续才会设计抽象层，本阶段不提前做；且会推翻 `docs/architecture/tech-stack.md:13` 的「SQLite 唯一真源」 | 需抽象连接/事务/全文/向量四类接口，FTS5（`db/schema.py:77`）与 Qdrant payload 过滤语义要对齐；`AGENTS.md:74-75` 分层规则需重审 |

---

## 3. 后续任务的建议拆分

| 阶段 | 建议内容 | 前置 |
|---|---|---|
| S1 | T2 / T7 / T8：计量口径、指标、按主体配额；先定维度与超额策略 | 主方案 P1–P3 落地，`audit_log` 已有主体维度 |
| S2 | T1 / A10：后端抽象与远程存储后端（pg + es + qdrant） | S1 的计量数据证明 SQLite 成为瓶颈；需先定抽象边界 |
| S3 | T3 / T4 / T5 / A8：镜像、CI、TLS、多副本与滚动升级 | S2 的远程存储可用；`RequestStateSecurity` 与配置一致性方案就绪 |
| S4 | A2 / A3：OIDC 或 MCP OAuth 2.1（若服务对外开放给第三方客户端） | 出现外部身份系统或公开 MCP 客户端诉求 |
| S5 | A6 / A7：外部队列或配置热更新（仅当 DB 认领或显式刷新被证明不足） | 有明确性能/运维证据，避免为抽象而抽象 |
| S6 | T11：固定 ruff 版本上界、全库统一重排，把 `ruff format --check` 纳入流水线 | 与 T3/T4 的镜像/CI 交付合并，避免单独制造大规模格式 diff |

---

## 4. 与主方案的对应关系

| 主方案位置 | 本文对应 |
|---|---|
| §0.1 拍板结论表 O3 / O8 / O10 | §1 的 T1 / T2 / T3 / T4 / T5 |
| §6.2「不做存储抽象」 | §1 T1、§2.6 A10 |
| §7.4「明确不做」 | §1 T2 / T7 / T8 |
| §8 DB 认领 | §2.3 A6 |
| §10.1 显式刷新 | §2.4 A7 / A7' |
| §9 同进程双应用 | §2.5 A8 / A9 |
| §5 自建用户表 + API Key | §2.2 A2 / A3 / A4 / A5 |
| §1.12 / §11.5 的 ruff 与语法门禁 | §1 T11、§3 S6 |
| §11.4 | 本文整体 |

---

## 6. 正式实现判定（基于本分析）

下列延迟任务不应继续无限期推迟，需要进入正式实现：

- **治理与计量**：T2、T8（按主体的配额、计量与文件资产核算）。
- **可观测性**：T7（结构化访问日志、指标出口与链路追踪基础）。
- **交付与可复现**：T3、T4（`uv.lock` 入库、Python 3.14 验证、镜像/compose/TLS/CI）。
- **质量门禁**：T11（固定 ruff 版本上界、全库统一格式、把 `ruff format --check` 纳入流水线）。

下列任务仅在触发条件满足时启动，当前不作为正式实现项：T1、T5、A1、A1'、A2、A3、A6、A7、A7'、A8、A10。下列任务按当前拍板结论不需要实现：T6、T9、T10、A4、A5、A9。

正式实现的修改方案见 `docs/plan/multi-user-production-formal-implementation.md`。本文仍只增不删；任务启动后在本节标注「已启动，见 <新方案路径>」。

> **已启动（2026-11）**：T2、T7、T8、T3、T4、T11 已按该方案进入实施，见
> `docs/plan/multi-user-production-formal-implementation.md`。本文其余判定（T1、T5、
> A1、A1'、A2、A3、A6、A7、A7'、A8、A10 触发启动；T6、T9、T10、A4、A5、A9 不实现）不变。

## 7. 维护约定

- 本文只增不删：任务一旦启动，移到新方案并在本文标注「已启动，见 <新方案路径>」，不删除原有证据与理由。
- 主方案随代码落地持续更新；本文同步维护「现状证据」的 `file:line`，避免引用过期（`AGENTS.md:164-166` 一次变更同步所有镜像）。
- 任一替代方案被启用时，必须同时说明它推翻了哪项既有决策（例如 A6 推翻 `docs/architecture/tech-stack.md:25`），并同步改写对应架构文档。

## 8. 现状更正

本文表格中的 `file:line` 是当时的历史基线，不再代表当前代码：`mcp_server/auth.py` 与静态 `mcp_token` 已移除，用户表、配额、WebUI 与部署文件已落地。当前权限边界与待修正问题以 `docs/plan/permission-hardening.md` 和 `docs/plan/permission-boundaries-repair.md` 为准。
