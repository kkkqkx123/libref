# Web 功能总览

Web 出口是查询面，另带反馈、任务、凭据与抓取写入。实现在 `src/libref/web/`，路由在 `app.py`，请求形状在 `api.py`。所有路由只做参数校验、服务调用与结果透传，业务逻辑在 `services/` 层。请求参数经由数据模型校验，取值范围与必填关系明确。

健康检查无需鉴权，其余路由统一经顶层中间件鉴权：dev 模式注入固定主体，enforced 模式按 API Key 解析主体并按 scope 授权，未授权与缺 scope 分别返回未授权与禁止。列表类查询统一使用条目加总数包络，检索另带降级标记与原因。参数错误返回请求错误，资源缺失返回未找到，审核流转失败按冲突与参数错误区分返回，注册表缺地址按未找到与网关错误区分返回。

错误响应统一为 `{"error": <稳定 code>, "detail": <原文>, "request_id": <编号>}`：`detail` 保持原样供人阅读与旧客户端消费，`error` 是前端据以本地化的标识。code 取自统一错误表，常见值有 `unauthorized/forbidden/missing_scope/origin_forbidden/not_found/page_not_found/job_not_found/quota_exceeded/review_conflict/index_request_conflict/invalid_argument/invalid_limit/invalid_offset/invalid_scope/invalid_ecosystem/chat_disabled/upstream_unavailable/unavailable/config_refresh_failed`；框架未标注的 404/405 分别回落到 `not_found` 与 `invalid_argument`，其余回落到 `unavailable`。配额拒绝额外在顶层给 `resource` 与 `reset_at`。

## 健康与目录

### GET /health

返回可用标记与配置指纹，不鉴权。

### GET /readyz

就绪探针，检查库可读写、向量与嵌入问答可用性，不鉴权。

### GET /me

返回当前主体的编号、名称、类型、租户、角色与 scope，需通过鉴权。

### GET /metrics

返回进程运行时间、延迟分位与请求、配额拒绝、token 用量等只读计数器，需 `read` scope。

### GET /catalog

`library` 可选，缺省返回库列表，给定返回该库版本列表，`ecosystem` 可选。版本项含 `registry_confirmed` 与 `prerelease`：前者表示注册表确认过，后者按版本后缀线索判定，供界面标注与过滤预发布，目录本身不隐藏任何版本。

## 检索问答

### GET /search

预设检索，`preset` 三选一：`precise` 纯文本单路，`balanced` 融合召回（默认），`thorough` 在均衡上打开窗口扩展。`thorough` 只补邻块、不扩大召回，需要更广召回时调大 `limit`。数值类旋钮全部归配置。

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| query | 是 | 无 | 查询词，非空 |
| library/version/ecosystem | 否 | 无 | 范围过滤 |
| limit | 否 | 8 | 1 到 100 的整数 |
| preset | 否 | balanced | `precise/balanced/thorough` |
| suggest | 否 | 关 | 空命中时附带注册表建议 |
| include_prerelease | 否 | 关 | 建议排序含预发布版本 |

返回 `items/count/degraded/reasons/answerable/answerability/cross_lingual`，字段语义与 MCP 检索一致；`cross_lingual` 标记全文单路因书写系统不匹配而零命中。`suggest` 且空命中时附带 `suggestions`，发现失败附带 `suggest_error`。

### GET /library-search

`query` 必填，`ecosystem` 可选，`limit` 取 1 到 100 默认 10，`include_prerelease` 可选。注册表失败返回网关错误。

### GET /web-search / GET /github-search

`query` 必填，`limit` 取 1 到 100 默认 10。结果未经核验，使用前需确认。注册表失败返回网关错误。

### GET /answer

预设检索后生成回答，返回回答文本、引用、降级标记与原因、模型名、置信度与引用编号。引用只保留回答实际引用的材料序号。`precise` 预设下不附带模式标签，保持零挖掘；`balanced/thorough` 在命中实体非空时附带至多三条不可引用的模式提示。材料总预算为硬上限，超出部分整条丢弃并以省略标记告知模型，引用编号与保留材料一一对应。预设优先级为调用方显式指定高于预设高于配置；问答不展开窗口。

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| query | 是 | 无 | 查询词，非空 |
| library/version/ecosystem | 否 | 无 | 范围过滤 |
| limit | 否 | 5 | 1 到 50 的整数 |
| preset | 否 | balanced | `precise/balanced/thorough` |

### GET /read

`mode` 三选一：`page` 整页，`window` 按中心序号取前后邻块，`section` 按来源分组编号取整节。

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| url | 是 | 无 | 文档地址，非空 |
| mode | 否 | page | `page/window/section` |
| sort_order | window 必填 | 无 | 中心序号 |
| radius | 否 | 1 | 0 到 20 的整数 |
| source_group_id | section 必填 | 无 | 来源分组编号 |
| library/version/ecosystem | 否 | 无 | 范围过滤 |

缺参返回请求错误，地址无命中返回未找到。

### GET /pages

按库版本列页面，供界面从目录进入阅读器（阅读器只按 `url` 寻址，需要先知道有哪些地址）。`library/version` 必填，`ecosystem` 可选，`limit` 取 1 到 200 默认 50，`offset` 非负。返回 `library/version/total/limit/offset/items`，条目含 `id/url/title/chunks`。越界参数按请求错误拒绝。

### GET /resolve

`name` 必填，`ecosystem/version` 可选，`target` 取 `docs/repo` 默认 `docs`，`include_prerelease` 可选。缺地址类失败返回未找到，其余注册表失败返回网关错误。

## 源码洞察

范围过滤 `library/version/ecosystem` 缺省跨生态。逗号分隔的多个值在服务端拆分为数组。`limit` 缺省沿用各视图默认值。

### GET /api-insight

`view` 四选一：`usage/related/args/common`。`related/args` 要求 `api`，`common` 要求 `api/apis` 之一，`apis` 为逗号分隔。

### GET /pattern-insight

`view` 十一选一：`patterns/evidence/tree/clusters/health/labels/snippets/facets/feature_summary/cluster_labels/drift`。带来源过滤的挖掘写入带指纹的独立档案，不改写默认 `usage`。`evidence` 要求非空 `pattern`（逗号分隔序列），`drift` 要求 `compare_profile`。`containing` 为逗号分隔的必含接口集合，`prefix` 为点分前缀，`facet` 按维度过滤形态，`per_file_limit` 限每文件证据数，`dedup` 三态（真/假/缺省跟随配置），`profile` 选择挖掘档案，缺省为 `usage`。

### GET /symbol-insight

`view` 四选一：`detail/file/children/signature`。`detail` 要求 `api/apis` 之一，其余三视图要求 `api`，`apis` 为逗号分隔。`offset` 默认 0。

### GET /call-insight

`view` 三选一：`callee/caller/file`，均要求 `api`。`include_ambiguous` 计入待定调用，`resolve_kinds` 为逗号分隔的解析来源，`relation_kinds` 为逗号分隔的调用形态（显式指定可放行宏形态），`min_args/max_args` 为实参数量区间。`offset` 默认 0。

### GET /import-insight

`view` 二选一：`importers/imports`，均要求 `api`。`offset` 默认 0。

### GET /repo-deps

`library/version` 非空必填，`ecosystem/dep_ecosystem` 可选。返回清单声明的依赖。

### GET /mining-health

`library/version/ecosystem` 全部可选，直接返回健康对象。

### GET /link-stats

`library/version` 必填，`ecosystem` 可选，`limit` 取 1 到 500 默认 20，给出 `url` 时返回单页统计，不给时返回版本汇总加被引用排名。版本定位不到返回请求错误。

## 治理

### GET /review

只支持列表动作，需 `review:read`。`action` 恒为 `list`，`limit` 默认 50，按调用方租户过滤。返回待定项包络。

### POST /review

请求体含 `action/entity_id/limit`，需 `review:write`。`action` 四选一：`list/approve/conflict/reopen`，非列表动作要求 `entity_id`。写操作校验行租户归属，跨租户按未找到拒绝。列表返回包络，非列表返回流转结果。非法流转返回冲突，缺参返回请求错误。

### POST /feedback

提交开发者反馈，需 `feedback:write`。请求体 `body` 必填，只记录不改审核状态。

### GET /feedback

查看反馈：`feedback:read` 看本租户全部，`feedback:read:own` 只看自己提交的条目；两者都没有返回禁止。`limit` 上限 200，`status` 可选过滤。

### POST /feedback/{feedback_id}/resolve

处置单条反馈，需 `feedback:resolve`。重复处置按未找到拒绝。

### GET /jobs / GET /jobs/{job_id}

`job_id` 缺省列最近任务，`limit` 取 1 到 200 默认 20，需 `read`，按调用方租户过滤。未知编号返回未找到。本节只读。详情在列表字段之外另给 `result` 与 `params`：后者是提交时的原始参数，失败任务据此可原样重提。

### POST /jobs

提交任务，需 `jobs:write`。请求体 `kind/params`，返回任务编号。

### POST /jobs/{job_id}/cancel

取消任务，需 `jobs:write`。未知编号返回未找到。

### GET /admin/keys

列出凭据，需 `admin:keys`。普通管理员只看本租户，平台管理员可看全部，`tenant_id` 可显式过滤；密钥明文不回显，列举动作记审计。

### GET /admin/principals

列出主体，需 `admin:keys`。返回 `id`、`tenant_id`、`kind`、`name`、`status`、`roles`、`scopes` 与 `conflicting`（每个违反职责分离的角色组；空数组表示合规）。普通管理员只看本租户，平台管理员可看全部；`tenant_id` 可显式过滤。该接口只读，不做任何修复，列举动作记审计。

### POST /admin/keys

创建主体并签发凭据，需 `admin:keys`。主体租户与 Key scope 都不得超过调用者权限，Key scope 也不得超过目标主体的角色 scope；非空 Key scope 会收窄角色权限，留空表示继承角色全部 scope。`developer` / `kb_admin` / `sys_admin` 三者互斥，同一主体只能持有其中一个（平台角色豁免）；未知角色、越权角色与冲突组合都返回请求错误。明文仅返回一次，默认有效期与最大有效期由 `[auth]` 控制。

### POST /admin/principals/{principal_id}/keys

给既有主体追加凭据，需 `admin:keys`。请求体只有 `scopes` 与 `expires_at`，沿用与签发相同的 scope 收窄与有效期约束；主体不可见时返回未找到，明文仅返回一次。批量轮换优先使用此接口，避免重复创建主体。

### DELETE /admin/keys/{key_id}

吊销凭据，需 `admin:keys`。只能吊销本租户主体的 Key，平台管理员不受此限；未知编号返回未找到。

### DELETE /admin/principals/{principal_id}/keys

吊销一个主体的全部有效 Key，需 `admin:keys`。返回吊销数量；主体不可见时返回未找到。

### GET /admin/quota/policies

列出配额策略，需 `admin:config`。普通管理员只看本租户，平台管理员可看全部；每项含租户、主体（空表示租户默认策略）、资源、窗口与上限。

### PUT /admin/quota/policies

写入或覆盖一条配额策略，需 `admin:config`。请求体给资源、上限、窗口，租户缺省落到调用者可写的租户，主体可选（必须属于目标租户，否则按请求错误拒绝）。写入后失效策略缓存并记审计；未知资源或窗口返回请求错误。

### POST /admin/config/refresh

重建冻结快照，需 `admin:config`。返回新旧指纹；上游密钥改环境变量后调本接口生效。配置解析或运行时重建失败返回错误并记审计，不再静默返回成功。

## 探查

只读预检，与抓取共用同一发现器与范围策略。参数错误返回请求错误。

### POST /doc-probe

请求体 `start_url` 必填，`scope` 为范围字典，`sample_limit` 取 1 到 100，`fetch_sample_limit` 取 0 到 100（0 表示禁用），`max_pages` 设预算上限（超限只标记不抛错），`prefilter` 先做存活探测。返回探查结构，超预算时带标记并给出实际规模，开启跟随链接时带膨胀提示。

### POST /repo-probe

探查结果中的 `role_candidates` 只给出 test/example 候选及命中来源（路径、注解、命名），不改变语料角色。

请求体 `source` 必填（远端地址或本地路径），`subdir` 可选，`max_files` 可选，`sample_limit` 取 1 到 100，`only_files` 为单文件限定数组（先过滤再做预算判断），`exclude_patterns` 为排除正则数组（与索引同口径），`tree_depth` 取 1 到 10 缺省 1，`samples_per_dir` 取 1 到 50（每目录采样上限，缺省关闭，总量受采样数约束）。返回目录结构、目录树（目录与递归文件数，最多 100 条）与按目录分组的采样，未命中的限定路径在返回体中回显。

## 写入

### POST /scrape

提交抓取任务，为命令行抓取的子集，需 `ingest:write`。请求体 `library/version/start_url` 必填，`ecosystem` 默认 `custom`，`max_pages` 可选。返回任务编号，执行由后台认领 worker 完成。归属按调用方租户标记。

### 索引申请

`/index-requests` 提供申请、列表、详情、撤回、审批与驳回；待审队列为 `GET /index-requests/pending`。返回请求编号和状态，审批时才提交 `scrape_docs` 或 `index_repo` 作业，作业编号写入请求参数。仓库申请可带 `source_role` 与 `target_filter`，分别声明语料角色和目标 API 前缀或限定名。审批会按当前设置重新校验目标地址；审批页展示完整规范化参数。撤回仅适用于作业运行前，已运行的作业返回冲突。详情包含关联作业、错误与驳回原因；终态只读，重新申请生成新行。持有 `ingest:write` 的申请方直接入队；无该 scope 的审批者不能审批自己的申请。通用 `/jobs` 不接受索引类作业，配额拒绝返回 `resource` 与 `reset_at`。

## 可配置项

完整参数定义见同目录 `config.md`（真相源），以下为按功能归纳。行为由配置决定，优先级为显式参数、环境变量、配置文件、内置默认。密钥只走环境变量。

* 服务暴露：`web_host/web_port` 为组合 `serve` 的唯一监听，`mcp_path` 为 `/mcp` 挂载路径。HTTP 与 `/mcp` 共用顶层鉴权中间件，健康检查与静态资源除外；`allowed_origins` 按来源精确匹配，非浏览器请求可不带 Origin。
* 检索问答：同 MCP 的向量、嵌入、精排、融合、片段、窗口、问答与发现注册表各键，详尽预设强制开窗口。
* 用法语料：`usage_mining_enabled/doc_code_mining_enabled` 控制源码文档注释与文档代码块是否进入 usage 档案。
* 洞察：`evidence_per_file_limit/evidence_dedup` 证据默认，`cluster_threshold/cluster_min_size` 分组默认，`facet_topn/facet_candidate_budget/facet_min_gain/guard_enabled` 形态选择，`induction_*` 标签归纳，`drift_topn` 漂移截断。
* 写入：抓取并发与超时、范围、分块、嵌入与向量后端各键，`snapshot_*` 快照与 `repos_dir/repos_quota_mb` 配额。
* 日志：`log_level/log_file/log_format`。

文档派生：`GET /doc-insight` 提供 `features` / `links` / `coverage` / `consistency` 四视图；`GET /search` 支持 `block_type`、`exclude_deprecated`、`aligned_only` 过滤。
