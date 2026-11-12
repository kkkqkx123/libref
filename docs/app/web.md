# Web 功能总览

Web 出口是只读查询面，另带精简抓取写入。实现在 `src/libref/web/`，路由在 `app.py`，请求形状在 `api.py`。所有路由只做参数校验、服务调用与结果透传，业务逻辑在 `services/` 层。请求参数经由数据模型校验，取值范围与必填关系明确。

健康检查无需鉴权，其余路由统一做令牌鉴权：携带配置令牌的请求放行，未携带返回未授权，鉴权要求开启但令牌缺失返回服务端配置错误。列表类查询统一使用条目加总数包络，检索另带降级标记与原因。参数错误返回请求错误，资源缺失返回未找到，审核流转失败按冲突与参数错误区分返回，注册表缺地址按未找到与网关错误区分返回。

## 健康与目录

### GET /health

返回可用标记与配置指纹，不鉴权。

### GET /catalog

`library` 可选，缺省返回库列表，给定返回该库版本列表，`ecosystem` 可选。

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

返回 `items/count/degraded/reasons`。`suggest` 且空命中时附带 `suggestions`，发现失败附带 `suggest_error`。

### GET /library-search

`query` 必填，`ecosystem` 可选，`limit` 取 1 到 100 默认 10，`include_prerelease` 可选。注册表失败返回网关错误。

### GET /web-search / GET /github-search

`query` 必填，`limit` 取 1 到 100 默认 10。结果未经核验，使用前需确认。注册表失败返回网关错误。

### GET /answer

预设检索后生成回答，返回回答文本、引用、降级标记与原因、模型名、置信度与引用编号。引用只保留回答实际引用的材料序号。`precise` 预设下不附带模式标签，保持零挖掘；`balanced/thorough` 在命中实体非空时附带至多三条标签。

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

## 治理

### GET /review

只支持列表动作。`action` 恒为 `list`，`limit` 默认 50。返回待定项包络。

### POST /review

请求体含 `action/entity_id/limit`。`action` 四选一：`list/approve/conflict/reopen`，非列表动作要求 `entity_id`。列表返回包络，非列表返回流转结果。非法流转返回冲突，缺参返回请求错误。

### GET /jobs / GET /jobs/{job_id}

`job_id` 缺省列最近任务，`limit` 取 1 到 200 默认 20。未知编号返回未找到。本出口不提交任务。

## 写入

### POST /scrape

精简抓取，为命令行抓取的子集，不含范围限定与断点续抓参数。请求体 `library/version/start_url` 必填，`ecosystem` 默认 `custom`，`max_pages` 可选。返回各结局计数。写入面仅此一处，探查、索引、挖掘与向量运维均不在本出口开放。

## 可配置项

完整参数定义见同目录 `config.md`（真相源），以下为按功能归纳。行为由配置决定，优先级为显式参数、环境变量、配置文件、内置默认。密钥只走环境变量。

* 服务暴露：`web_host/web_port` 监听地址，鉴权复用 `mcp_token/mcp_require_auth`（令牌只走环境；鉴权要求开启但令牌缺失返回服务端配置错误）。
* 检索问答：同 MCP 的向量、嵌入、精排、融合、片段、窗口、问答与发现注册表各键，详尽预设强制开窗口。
* 洞察：`evidence_per_file_limit/evidence_dedup` 证据默认，`cluster_threshold/cluster_min_size` 分组默认。
* 写入：抓取并发与超时、范围、分块、嵌入与向量后端各键，`snapshot_*` 快照与 `repos_dir/repos_quota_mb` 配额。
* 日志：`log_level/log_file/log_format`。
