# Web 功能总览

Web 出口是查询面，另带反馈、任务、凭据与抓取写入。实现在 `src/libref/web/`，路由在 `app.py`，请求形状在 `api.py`。所有路由只做参数校验、服务调用与结果透传，业务逻辑在 `services/` 层。请求参数经由数据模型校验，取值范围与必填关系明确。

健康检查无需鉴权，其余路由统一经顶层中间件鉴权：dev 模式注入固定主体，enforced 模式按 API Key 解析主体并按 scope 授权，未授权与缺 scope 分别返回未授权与禁止。列表类查询统一使用条目加总数包络，检索另带降级标记与原因。参数错误返回请求错误，资源缺失返回未找到，审核流转失败按冲突与参数错误区分返回，注册表缺地址按未找到与网关错误区分返回。

错误响应统一为 `{"error": <稳定 code>, "detail": <原文>, "request_id": <编号>}`：`detail` 保持原样供人阅读与旧客户端消费，`error` 是前端据以本地化的标识。已知 code 有 `unauthorized/forbidden/missing_scope/origin_forbidden/not_found/page_not_found/job_not_found/quota_exceeded/review_conflict/invalid_limit/invalid_offset/invalid_scope/unavailable`，未归类的原因回落到 `http_<状态码>`。配额拒绝额外在顶层给 `resource` 与 `reset_at`。

## 健康与目录

### GET /health

返回可用标记与配置指纹，不鉴权。

### GET /readyz

就绪探针，检查库可读写、向量与嵌入问答可用性，不鉴权。

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

返回 `items/count/degraded/reasons/answerable/answerability`，`answerable` 语义与 MCP 检索一致。`suggest` 且空命中时附带 `suggestions`，发现失败附带 `suggest_error`。

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

`view` 七选一：`patterns/evidence/tree/clusters/health/labels/snippets`。`evidence` 要求非空 `pattern`（逗号分隔序列）。`containing` 为逗号分隔的必含接口集合，`prefix` 为点分前缀，`per_file_limit` 限每文件证据数，`dedup` 三态（真/假/缺省跟随配置）。

### GET /symbol-insight

`view` 四选一：`detail/file/children/signature`。`detail` 要求 `api/apis` 之一，其余三视图要求 `api`，`apis` 为逗号分隔。`offset` 默认 0。

### GET /call-insight

`view` 三选一：`callee/caller/file`，均要求 `api`。`include_ambiguous` 计入待定调用，`resolve_kinds` 为逗号分隔的调用种类，`min_args/max_args` 为实参数量区间。`offset` 默认 0。

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

查看本租户反馈，需 `feedback:read`。`limit` 上限 200，`status` 可选过滤。

### POST /feedback/{feedback_id}/resolve

处置单条反馈，需 `feedback:resolve`。重复处置按未找到拒绝。

### GET /jobs / GET /jobs/{job_id}

`job_id` 缺省列最近任务，`limit` 取 1 到 200 默认 20，需 `read`，按调用方租户过滤。未知编号返回未找到。本节只读。详情在列表字段之外另给 `result` 与 `params`：后者是提交时的原始参数，失败任务据此可原样重提。

### POST /jobs

提交任务，需 `jobs:write`。请求体 `kind/params`，返回任务编号。

### POST /jobs/{job_id}/cancel

取消任务，需 `jobs:write`。未知编号返回未找到。

### GET /admin/keys

列出凭据，需 `admin:keys`。普通管理员只看本租户，平台管理员可看全部；密钥明文不回显，列举动作记审计。

### POST /admin/keys

签发凭据，需 `admin:keys`。主体租户与 Key scope 都不得超过调用者权限，Key scope 也不得超过目标主体的角色 scope；非空 Key scope 会收窄角色权限，留空表示继承角色全部 scope。未知 scope 与越权 scope 返回请求错误。明文仅返回一次，默认有效期与最大有效期由 `[auth]` 控制。

### DELETE /admin/keys/{key_id}

吊销凭据，需 `admin:keys`。只能吊销本租户主体的 Key，平台管理员不受此限；未知编号返回未找到。

### DELETE /admin/principals/{principal_id}/keys

吊销一个主体的全部有效 Key，需 `admin:keys`。返回吊销数量；主体不可见时返回未找到。

### POST /admin/config/refresh

重建冻结快照，需 `admin:config`。返回新旧指纹；上游密钥改环境变量后调本接口生效。配置解析或运行时重建失败返回错误并记审计，不再静默返回成功。

## 探查

只读预检，与抓取共用同一发现器与范围策略。参数错误返回请求错误。

### POST /doc-probe

请求体 `start_url` 必填，`scope` 为范围字典，`sample_limit` 取 1 到 100，`fetch_sample_limit` 取 0 到 100（0 表示禁用），`max_pages` 设预算上限（超限只标记不抛错），`prefilter` 先做存活探测。返回探查结构，超预算时带标记并给出实际规模，开启跟随链接时带膨胀提示。

### POST /repo-probe

请求体 `source` 必填（远端地址或本地路径），`subdir` 可选，`max_files` 可选，`sample_limit` 取 1 到 100，`only_files` 为单文件限定数组（先过滤再做预算判断），`exclude_patterns` 为排除正则数组（与索引同口径），`tree_depth` 取 1 到 10 缺省 1，`samples_per_dir` 取 1 到 50（每目录采样上限，缺省关闭，总量受采样数约束）。返回目录结构、目录树（目录与递归文件数，最多 100 条）与按目录分组的采样，未命中的限定路径在返回体中回显。

## 写入

### POST /scrape

提交抓取任务，为命令行抓取的子集，需 `ingest:write`。请求体 `library/version/start_url` 必填，`ecosystem` 默认 `custom`，`max_pages` 可选。返回任务编号，执行由后台认领 worker 完成。归属按调用方租户标记。

### 索引申请

`/index-requests` 提供申请、列表、详情、撤回、审批与驳回；待审队列为 `GET /index-requests/pending`。返回请求编号和状态，审批后才提交 `scrape_docs` 或 `index_repo` 作业，作业编号写入请求参数。详情包含关联作业、错误与驳回原因；终态只读，重新申请生成新行。持有 `ingest:write` 的申请方直接入队；无该 scope 的审批者不能审批自己的申请。通用 `/jobs` 不接受索引类作业。

## 可配置项

完整参数定义见同目录 `config.md`（真相源），以下为按功能归纳。行为由配置决定，优先级为显式参数、环境变量、配置文件、内置默认。密钥只走环境变量。

* 服务暴露：`web_host/web_port` 为组合 `serve` 的唯一监听，`mcp_path` 为 `/mcp` 挂载路径。HTTP 与 `/mcp` 共用顶层鉴权中间件，健康检查与静态资源除外；`allowed_origins` 按来源精确匹配，非浏览器请求可不带 Origin。
* 检索问答：同 MCP 的向量、嵌入、精排、融合、片段、窗口、问答与发现注册表各键，详尽预设强制开窗口。
* 洞察：`evidence_per_file_limit/evidence_dedup` 证据默认，`cluster_threshold/cluster_min_size` 分组默认。
* 写入：抓取并发与超时、范围、分块、嵌入与向量后端各键，`snapshot_*` 快照与 `repos_dir/repos_quota_mb` 配额。
* 日志：`log_level/log_file/log_format`。
