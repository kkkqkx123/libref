# 索引请求闭环：独立阶段设计

## 定位

本文作为下一阶段独立任务的设计基线，目标是让 AI 客户端和普通开发者都能以**申请**方式请求仓库或文档库索引，经审批后由服务端排队执行，申请方可等待完成并立即读取结果。本阶段不改造认证体系本身，只复用已落地的 scope、租户、配额、审计与任务机制。

## 1. 现状分析

| 现有能力 | 位置 | 缺口 |
|---|---|---|
| 文档抓取入库 `POST /scrape` | `web/app.py` | 需要 `ingest:write`；`external/developer/kb_admin` 无权调用；无审批、无请求实体 |
| 任务提交/查询 `POST /jobs`、`GET /jobs/{id}` | `web/app.py` | 需要 `jobs:write` 才能提交；`job_show` 只读；无“申请—审批—入库”状态机 |
| 仓库索引任务 `INDEX_REPO_JOB_KIND`、`build_repo_runner` | `services/code/code_ingest.py` | 仅 CLI `--async` 本地路径使用；组合 `serve` 只注册了抓取 runner，仓库索引作业在服务侧无人消费 |
| CLI `ingest`、`index-repo` | `cli/docs_commands.py`、`cli/code_commands.py` | 本地直连或远程 `/scrape`，均是特权操作；没有低权限申请命令与审批命令 |
| MCP 发现与探针：`github_search`、`library_search`、`resolve`、`repo_probe`、`doc_probe` | `mcp_server/server.py` | 能发现和验证目标，但没有提交索引请求、查询申请状态、等待完成或审批的工具 |
| 审核 `review_status` 与 `review` 工具/页面 | `services/ops/review.py`、`webui/src/views/ReviewView.vue` | 面向 call instance 的质量审核，不是索引准入审批；语义不同，不复用 |
| 反馈 `feedback` | `services/ops/feedback.py` | 只记录意见，不驱动作业，也无审批状态 |

结论：发现、探针、读取已经形成基础，但“提交请求 — 审批 — 执行 — 回读”链路缺失；低权限角色没有任何可被 AI 或人使用的申请入口。

## 2. 设计决策

- 新增独立业务对象 `index_requests`，不复用 `feedback` 或 `review_status`，避免把意见与质量审核混成准入审批。
- 执行仍复用 `jobs` 表与 `JobManager`：文档请求审批后提交 `scrape_docs`，仓库请求审批后提交 `index_repo`；请求记录保存 `job_id` 作为执行关联。
- 组合 `serve` 的工作进程注册 `build_scrape_runner` 与 `build_repo_runner`，使两类已审批作业都能被消费。其他作业种类维持现状。
- 新增两个 scope：`index:request`（提交、查看自己的请求、取消自己的待审请求）与 `index:approve`（查看待审队列、通过或驳回）。
- 自审批不新增 scope，而由更强的写权限派生：持有 `ingest:write` 的主体本来就能直接入库，禁止其审批自己的申请只会制造绕过。创建请求时若持有 `ingest:write`，直接进入 `approved`/`queued` 并写自审批审计。
- 只持有 `index:approve` 而无 `ingest:write` 的审批者（如 `kb_admin`）不得审批自己的申请，保留职责分离。`jobs:write` 不派生自审批；通用 `/jobs` 入口需限制索引类作业，避免绕过审批。
- 当前不引入 `index:auto_approve`。若后续出现无人值守机器主体需要免人工审批，再以该 scope 显式授权并要求同时持有 `index:request`。
- 远程申请只接受可校验的 HTTP(S) Git 地址或可解析的仓库坐标；本地路径只允许本机 CLI 直连索引，禁止经 API/MCP 提交。
- 等待采用有界轮询，不做无限阻塞；终态后由读取工具接续，形成闭环。

## 3. scope 与角色映射

| 角色 | 现有 scope 增量 | 申请 | 审批 | 自审批 |
|---|---|---|---|---|
| `external` | `index:request` | 是 | 否 | 否 |
| `developer` | `index:request` | 是 | 否 | 否 |
| `kb_admin` | `index:request`、`index:approve` | 是 | 是（不能审批自己的请求） | 否 |
| `sys_admin` | `index:request`、`index:approve` | 是 | 是 | 是（由 `ingest:write` 派生） |
| `platform_admin` | 由角色并集自动获得 | 是 | 是 | 是（由 `ingest:write` 派生） |

`KNOWN_SCOPES` 与 `ROLE_SCOPES` 同步扩展；`index:request`/`index:approve` 纳入闭合集合，Key scope 仍只能收窄。已有 Key 留空 scope 时自动继承新 scope，无需数据迁移。

## 4. 请求模型与状态机

新增迁移，在 `db/schema.py` 与 `db/migrations.py` 建立 `index_requests`：租户、申请主体、类型（`repo`/`docs`）、规范化目标、来源地址、参数 JSON、状态、审批人、驳回原因、关联 `job_id`、时间戳与去重键。按 `(tenant_id, dedupe_key)` 建索引，按租户与申请主体建查询索引。

状态流转：

- `pending`：提交成功，等待审批；可由申请方取消。持有 `ingest:write` 的申请方创建时不进入此状态，直接进入 `approved`/`queued`。
- `approved` → `queued`：审批通过并成功提交作业；作业编号写入请求。
- `running`：作业被 worker 领取。
- `done`：索引完成，可读取。
- `failed`：作业失败，携带错误信息，可重新申请。
- `rejected`：审批驳回，携带原因。
- `cancelled`：申请方取消或关联作业被取消。

审批通过后若作业提交失败，请求回到 `pending` 并记录失败原因，便于重试；终态只读，重新申请生成新行。同一租户内 `pending/queued/running` 且去重键相同的请求直接复用已有编号，避免重复排队。

## 5. 后端实现

- 新服务 `services/ops/index_requests.py` 负责参数规范化、去重、状态流转、审批约束、作业提交与终态回写；它是编排层，不直接定义 SQL 底层。
- 参数校验：文档复用 `validate_scrape_target`；仓库复用 `sanitize_http_url` 与 `allowed_hosts`/私有地址规则，拒绝本地路径与非常规协议。仓库坐标可先经 `services/registry/resolver.py` 解析为 URL，解析来源写入请求参数以便复现。
- API 新增于 `web/app.py`：`POST /index-requests`、`GET /index-requests`、`GET /index-requests/{id}`、`POST /index-requests/{id}/cancel`、`POST /index-requests/{id}/approve`、`POST /index-requests/{id}/reject`、`GET /index-requests/pending`。请求模型放 `web/api.py`。
- 申请方仅能查看和取消自己的请求；`index:approve` 可查看待审队列并执行审批；审批时校验审批人不是申请人，除非审批人持有 `ingest:write`。平台范围遵循现有租户可见性规则。
- 作业执行器在既有 `build_scrape_runner`、`build_repo_runner` 外层增加请求回写，成功/失败都调用 `IndexRequestService.finish()`；请求编号写入作业参数以保证可重放。
- 收紧通用 `POST /jobs`：索引类作业只能由 `index_requests` 服务提交；普通 `jobs:write` 调用提交这些种类返回 400，避免审批旁路。已有 `ingest:write` 的直接入库路径不经过该通用入口。
- 配额新增 `index_requests` 资源，按主体限制申请频率；审批通过后的作业继续计入 `job_submissions`、`scrape_pages`、`repos_bytes`。配额策略仍以数据库为准、配置兜底 0 表示不限。
- 审计动作：`index_request.create`、`index_request.approve`、`index_request.reject`、`index_request.cancel`、`index_request.complete`、`index_request.auto_approve`。租户与主体沿用现有归一化规则。

## 6. MCP 工具

新增工具与 scope 绑定如下；它们不进入现有 `read` 工具包装集合，需像 `review`、`submit_feedback` 一样单独声明自己的 scope。

| 工具 | 所需 scope | 作用 |
|---|---|---|
| `request_repo_index` | `index:request` | 按 ecosystem/library/version 或显式 Git URL 提交仓库索引申请 |
| `request_docs_index` | `index:request` | 按 library/version/start_url 提交文档索引申请 |
| `index_request_status` | `read` | 按请求编号返回状态、关联作业编号、错误信息与建议的后续读取工具 |
| `wait_index_request` | `read` | 有界轮询到终态或超时；超时返回当前状态而不是抛错 |
| `list_index_requests` | `index:request` | 查看自己的申请；审批角色可附加待审过滤 |
| `review_index_request` | `index:approve` | 列表、通过、驳回；动作与请求编号必填 |

AI 闭环顺序：`github_search`/`library_search`/`resolve` 发现目标 → `repo_probe`/`doc_probe` 验证范围 → `request_repo_index`/`request_docs_index` → 返回请求编号 → `wait_index_request`（或 `index_request_status` 轮询）→ `done` 后调用 `catalog`、`search`、`api_insight` 等既有读取工具。工具输出中给出下一步工具名，减少客户端猜解。

## 7. CLI

新增 `cli/request_commands.py`，保持 CLI 只做参数解析、服务调用与格式化：

- `request submit --kind repo|docs ...`：提交申请，打印请求编号与状态。
- `request list`、`request show <id>`：查看自己的申请或待审队列。
- `request approve <id>`、`request reject <id> --reason ...`、`request cancel <id>`：审批与撤回。
- `request wait <id> --timeout 300`：复用 `JobManager.wait_for` 的等待语义，终态退出码与现有远程 ingest 一致。

`--remote` 通过 `cli/remote.py` 的 Bearer 头调用新 API；本地直连模式同样走服务层，不绕过审批。现有 `ingest`、`index-repo` 保留为特权直接索引入口，不承担低权限申请职责。

## 8. 前端

新增两个页面并挂到现有 SPA，不新建应用：

- `/requests`：申请入口与“我的申请”列表。表单按 `repo`/`docs` 切换字段，提交前可调用现有 `doc-probe`/`repo-probe` 预览；列表展示状态、关联作业、驳回原因与重新申请入口；路由 scope 为 `index:request`。
- `/requests/approvals`：审批队列，展示目标、来源、申请人与时间；通过/驳回按钮按 `index:approve` 控制；驳回必须填写原因。

同步修改 `webui/src/router/index.ts`、`AppSidebar.vue`、`api/types.ts`、`api/client.ts`、中英文 i18n 与状态徽标；复用现有 `Envelope` 与错误码约定。

## 9. 验证口径

- 单元测试：去重规则、状态流转非法跃迁、无 `ingest:write` 审批者自审批拒绝、有 `ingest:write` 自审批直接入队、scope 展开、参数校验、有界等待。
- 集成测试：低权限 Key 提交成功且作业在审批前不运行；审批后作业完成并回写 `done`；驳回与取消不产生作业；MCP 闭环工具串行调用；CLI `--remote` 同一路径。
- 前端测试：路由 scope 门禁、审批按钮可见性、列表终态展示。
- 黄金用例：仓库与文档各一条“发现 → 申请 → 审批 → 等待 → 读取”闭环，断言读取结果来自新索引。

## 10. 明确不做

- 不引入自动审批、第三方身份、MCP OAuth 或新的会话体系。
- 不把索引申请塞进 `feedback` 或 call instance `review_status`。
- 不开放远程本地路径索引，不取消审批直连入库。
- 不改变既有 Key 生命周期、租户隔离与审计写入原则。

相关现状文档：`docs/features/web/permissions.md`、`docs/app/mcp.md`、`docs/app/cli.md`、`docs/architecture/module-layout.md`。
