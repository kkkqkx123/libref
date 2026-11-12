# CLI 功能总览

命令行是全量管理入口，覆盖写入、读取与运维。实现在 `src/libref/cli/`，参数定义在 `parsers.py`，命令实现分置于 `docs_commands.py`、`code_commands.py`、`registry_commands.py`、`ops_commands.py`，公共装配在 `common.py`。所有命令只做参数解析、服务调用与结果打印，业务逻辑在 `services/` 层。

命令成功返回 0，运行失败返回 1，用法或配置错误返回 2，参数错误信息打印到标准错误。

## 全局选项

作用于全部命令。`--config` 指定配置文件路径，缺省时按当前目录、用户配置目录顺序查找。`--log-level` 取 `DEBUG/INFO/WARNING/ERROR` 之一。`--log-file` 指定日志文件，缺省只写标准错误。`--log-format` 取 `text/json`。四者均覆盖配置文件中的同名项。

## 通用约定

* 范围过滤：`library/version/ecosystem` 只做结果限定，不参与调参。缺省表示不限定。
* 两种来源形态：显式形态直接给出地址或路径；解析形态按生态加包名查注册表得到地址与版本。`ingest` 与 `index-repo` 支持两种形态，不可混用。
* 异步任务：带 `--async` 的写入命令经任务队列执行，输出任务编号与最终状态，失败返回 1。
* 调试覆盖：`search/answer` 的 `--expand-window/--window-radius/--material-limit` 只覆盖本次运行的配置值，不属于模型可见的工具参数。

## 文档写入

### ingest

按文档站抓取入库。显式形态位置参数为 `library/version/start_url` 三件套；解析形态给出 `--ecosystem` 加包名（`library` 位置参数此时为包名），`--version` 缺省取稳定版，`start_url` 不可与 `--ecosystem` 同给。

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| library | 是 | 无 | 显式形态为库名，解析形态为包名 |
| version | 显式形态是 | 无 | 解析形态缺省取稳定版 |
| start_url | 显式形态是 | 无 | 与 `--ecosystem` 互斥 |
| --ecosystem | 否 | 无 | 解析生态，`pypi/npm/maven/go/crates/nuget`，`maven` 坐标形如 `group:artifact` |
| --include-prerelease | 否 | 关 | 解析时含预发布版本 |
| --offline | 否 | 关 | 只读缓存，不联网 |
| --registry-mirror | 否 | 配置值 | 注册表镜像预设 |
| --max-pages | 否 | 不限 | 抓取页数上限 |
| --url | 否 | 无 | 可重复，单页目标，仍受范围过滤 |
| --fresh | 否 | 关 | 丢弃断点游标，全量重跑 |
| --prefilter | 否 | 关 | 先发存活探测再抓取 |
| --async | 否 | 关 | 经任务队列执行 |
| --allow-cross-site | 否 | 关 | 允许跨站 |
| --allow-subdomains | 否 | 关 | 允许同域子域 |
| --path-prefix/--path-suffix | 否 | 无 | 可重复，路径前后缀限定 |
| --include-pattern/--exclude-pattern | 否 | 无 | 可重复，正则允许/拒绝 |
| --max-depth | 否 | 无 | 相对起始地址的最大深度 |
| --render-js | 否 | 关 | 疑似渲染页回退浏览器抓取 |
| --render-wait-for | 否 | 无 | 浏览器等待的选择器 |
| --render-timeout-ms | 否 | 配置值 | 浏览器超时毫秒 |
| --follow-links | 否 | 关 | 页内链接广度跟随 |
| --crawl-query | 否 | 无 | 按地址相关性排序抓取目标 |
| --score-threshold | 否 | 无 | 抓取目标打分阈值 |
| --bypass-cache | 否 | 关 | 跳过条件请求头强制重抓 |

成功按结局打印计数。相关配置：抓取并发与超时、礼貌延迟、浏览器开关与超时、种子缓存、范围与分块、嵌入与向量后端、快照与配额。

### refresh

`library/version/start_url` 三位置参数必填，其余抓取语义与 `ingest` 一致，支持同步与 `--async` 两种执行。按增量语义复抓，输出同为结局计数。

### smoke-local

无参数。起临时站点验证发现到存储全链，不依赖外部网络，`stored` 计数大于零即成功。

## 源码写入

### index-repo

索引仓库并落调用实例。显式形态位置参数为 `library/version/source`，`source` 为远端地址或本地路径；解析形态按 `--ecosystem` 加包名解析仓库地址，此时 `source` 不可给出，`version` 缺省取稳定版。

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| library | 是 | 无 | 显式形态为库名，解析形态为包名 |
| version | 显式形态是 | 无 | 解析形态缺省取稳定版 |
| source | 显式形态是 | 无 | 与 `--ecosystem` 互斥 |
| --ecosystem | 否 | 无 | 解析生态 |
| --include-prerelease | 否 | 关 | 解析时含预发布版本 |
| --offline | 否 | 关 | 只读缓存 |
| --registry-mirror | 否 | 配置值 | 注册表镜像预设 |
| --update | 否 | 关 | 已存在仓库拉取更新 |
| --async | 否 | 关 | 经任务队列执行 |
| --subdir | 否 | 无 | 只索引该子目录 |
| --only-file | 否 | 无 | 可重复，只索引该仓内文件 |
| --max-files | 否 | 无 | 文件数上限 |
| --fresh | 否 | 关 | 丢弃断点游标 |

成功打印计数。相关配置：仓库目录与配额、抓取并发与超时、嵌入与向量后端。

### mine

按库与版本重建闭合调用序列、特征树与分组，输出每条 `支持度 模式`。

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| library/version | 是 | 无 | 目标库版本 |
| --ecosystem | 否 | 无 | 缺省跨生态解析 |
| --minsup | 否 | 2 | 最小支持度 |
| --topn | 否 | 无 | 按调用次数保留限定名前列 |
| --include-ambiguous | 否 | 关 | 序列中计入待定调用 |
| --maxlen/--minlen | 否 | 10/2 | 序列长度区间 |
| --cluster-threshold | 否 | 0.15 | 聚类阈值，对应 `cluster_threshold` |
| --cluster-min-size | 否 | 2 | 聚类最小规模，对应 `cluster_min_size` |
| --resolve-kinds | 否 | 无 | 逗号分隔的调用种类过滤 |
| --min-args/--max-args | 否 | 无 | 实参数量区间过滤 |
| --async | 否 | 关 | 经任务队列执行 |

### induce-labels

调用对话能力为模式生成标签并输出 JSON。`library/version` 必填，`--ecosystem` 可选，`--topn` 缺省取配置，支持 `--async`。对话未配置时失败。

## 检索问答

### search

预设检索，`preset` 三选一：`precise` 纯文本单路，`balanced` 融合召回（默认），`thorough` 在均衡上打开窗口扩展。`thorough` 只补邻块、不扩大召回，需要更广召回时调大 `--limit`。

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| query | 是 | 无 | 查询词，非空 |
| --library/--version/--ecosystem | 否 | 无 | 范围过滤 |
| --limit | 否 | 8 | 返回条数，正整数 |
| --preset | 否 | balanced | `precise/balanced/thorough` |
| --expand-window/--window-radius | 否 | 无 | 本地调试覆盖配置值 |
| --suggest | 否 | 关 | 空命中时给出注册表建议 |
| --include-prerelease | 否 | 关 | 建议排序含预发布版本 |

输出每条的分数、标题、地址与片段，窗口命中附带邻块。降级时标准错误提示原因。`limit` 上限由服务层校验（正整数），越界由 MCP/Web 侧另行约束。

### answer

预设检索后组装材料并生成回答。参数与 `search` 同形，`--limit` 默认 5，`--material-limit` 为本地调试覆盖。

```text
answer query [--library ...] [--version ...] [--ecosystem ...] [--limit 5] [--preset balanced]
```

无证据打印 `(no evidence)` 并返回 0。成功打印回答正文与引用，降级或低置信（回答未引用任何材料）时标准错误提示。`precise` 预设下回答不附带模式标签，保持零挖掘；`balanced/thorough` 在命中实体非空时附带至多三条标签。

### read

按地址读取文档，`mode` 三选一：`page` 整页，`window` 按中心序号取前后邻块，`section` 按来源分组编号取整节。

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| url | 是 | 无 | 文档地址 |
| --mode | 否 | page | `page/window/section` |
| --sort-order | window 必填 | 无 | 中心序号 |
| --radius | 否 | 1 | 邻块半径 |
| --source-group-id | section 必填 | 无 | 来源分组编号 |
| --library/--version/--ecosystem | 否 | 无 | 范围过滤 |

参数缺失返回 2，地址无命中返回 1。

## 发现目录

### discover

按查询词跨注册表模糊发现候选。`query` 必填，`--ecosystem` 可选，`--limit` 默认 10，`--include-prerelease` 可选。输出候选 JSON，失败打印分类错误。

### search-web / search-github

显式搜索，结果未经核验。`query` 必填，`--limit` 默认 10。输出同 `discover`。

### catalog

`library` 可选，缺省返回库列表，给定返回该库版本列表，`--ecosystem` 可选。

### resolve

`name` 必填，`--ecosystem/--version` 可选，`--target` 取 `docs/repo` 默认 `docs`，`--include-prerelease` 可选。输出解析 JSON，失败打印分类错误与后续动作。

## 探查预览

### probe-docs

只返回站点结构，不落库。`start_url` 必填，`--sample-limit/--fetch-sample-limit` 可选（后者 0 表示禁用），范围参数与 `ingest` 一致（`--url/--max-depth/--crawl-query/--score-threshold` 等）。

### probe-repo

只返回目录结构，不解析。`source` 必填，`--subdir/--max-files/--sample-limit` 可选。

### dump-chunks

预览分块。`--markdown/--source` 二选一必填，`--url` 为文档块的地址注解。

## 源码洞察

### api-insight

`view` 四选一：`usage` 频次榜，`related` 同现接口，`args` 参数形状，`common` 多接口公共调用。`related/args` 要求 `--api`，`common` 要求 `--api/--apis` 之一。`--apis` 为逗号分隔。范围过滤与 `--limit` 可选，缺省沿用视图默认值。

### pattern-insight

`view` 七选一：`patterns/evidence/tree/clusters/health/labels/snippets`。`evidence` 要求 `--pattern` 非空（逗号分隔序列）。`--containing` 为逗号分隔的必含接口集合，`--prefix` 按点分前缀过滤，`--per-file-limit` 限每文件证据数。`--dedup/--no-dedup` 互斥，分别强制开关注入去重，都不给表示跟随配置。范围过滤与 `--limit` 可选。

### symbol-insight

`view` 四选一：`detail/file/children/signature`。`detail` 要求 `--api/--apis` 之一，其余三视图要求 `--api`。`--limit` 可选，`--offset` 默认 0。

### call-insight

`view` 三选一：`callee/caller/file`，均要求 `--api`。`--include-ambiguous` 计入待定调用，`--resolve-kinds` 为逗号分隔的调用种类，`--min-args/--max-args` 为实参数量区间。`--limit/--offset` 可选。

### import-insight

`view` 二选一：`importers/imports`，均要求 `--api`。`--limit/--offset` 可选。

### repo-deps

`library/version` 必填，`--ecosystem/--dep-ecosystem` 可选。输出清单声明的依赖。

### mining-health

复用度与多样性信号，`--library/--version/--ecosystem` 全部可选。

## 运维

* `init-db`：建库并应用迁移，无参数，打印库路径与已应用迁移数。
* `reembed`：`library/version` 必填，`--ecosystem` 可选。全量按真源重嵌，嵌入未配置返回 1。
* `reconcile`：参数同 `reembed`。按集合差修复单版本向量，输出真源数、向量数、缺失、孤儿、写入与重建数。库版本必须解析到唯一版本。
* `fts-rebuild/fts-check`：无参数。输出文档数、采样数、缺失数与同步标记，不一致时返回 1。
* `review`：`list [--limit 50]` 列待定项；`approve/conflict/reopen entity_id` 流转单实体。非法流转返回 1。
* `job-show`：`job_id` 可选，`--list` 列最近任务，`--limit` 默认 20，`--full` 打印完整结果。未知编号返回 1。
* `config-show`：无参数。打印配置文件路径、配置指纹与脱敏后的有效配置。
* `purge`：`library/version` 必填，`--ecosystem` 可选。整版本删除并记录，输出各计数。
* `deletions`：`--limit` 默认 50，`--full` 打印详情，`--prune` 按保留期清理，`--ttl-days` 缺省取配置。

## 服务启动

* `serve-mcp`：`--http` 切换为流式 HTTP（默认标准输入输出），`--host/--port/--path` 缺省取配置，`--enabled-tools/--disabled-tools` 为逗号分隔的工具白名单与黑名单。非回环监听且无令牌时启动失败返回 2。
* `serve-web`：`--host/--port` 缺省取配置，鉴权语义与 MCP 的 HTTP 形态一致。

## 可配置项

完整参数定义见同目录 `config.md`（真相源），以下为按功能归纳。命令行行为由配置文件与环境变量共同决定，优先级为显式参数、环境变量、配置文件、内置默认。密钥只走环境变量，不可写入文件。`config-show` 打印脱敏后的有效快照。

* 服务：`db_path` 库路径，`heartbeat_timeout` 任务心跳超时。
* 抓取：`fetch_concurrency/request_timeout/fetch_max_retries/crawl_delay_default/robots_cache_ttl_days/user_agent`，代理 `http_proxy/no_proxy`，浏览器 `render_js_default/render_wait_for/render_timeout_ms`，`seeder_cache_ttl_hours/follow_links_default`，`ingest_clean_cache_cap/ingest_batch_pages/index_batch_files/probe_sample_limit/probe_cache_ttl_hours/probe_fetch_sample_limit/probe_fetch_timeout`。
* 存储：`vector_backend` 取 `simvec/qdrant`，`simvec_dir/simvec_lib` 本地引擎位置，`qdrant_url/vector_collection/qdrant_timeout` 远端集合。
* 分块：`chunk_embed_*` 嵌入轨道分块预算，`chunk_bm25_*` 全文轨道分块预算，`chunk_reserve_ratio` 预留比。
* 嵌入：`embedding_model/embedding_dim/embedding_api_base/embed_batch`，密钥 `embedding_api_key` 只走环境。
* 精排：`rerank_model/rerank_enabled/rerank_candidate_limit/rerank_top_n/rerank_truncate`，其中 `rerank_top_n` 不得超过 `rerank_candidate_limit`。
* 检索：`rrf_k/rrf_w_fts/rrf_w_vec` 融合权重，`freq_alpha/cluster_alpha` 挖掘加权，`candidate_multiplier/assemble_multiplier/assemble_min` 召回与组装预算，`retrieval_max_versions` 跨版本扇出上限，`snippet_radius/snippet_cap` 片段形态（前者须小于后者），`window_preview_cap` 窗口预览上限，`fts_w_content/fts_w_title/fts_w_url/fts_w_path` 全文列权重，`cluster_threshold/cluster_min_size` 聚类，`retriever_window_radius/retriever_expand_window` 窗口默认，详尽预设强制开窗口。
* 问答：`material_limit` 单材料上限，`answer_total_budget` 回答总预算，`chat_model/chat_enabled/chat_api_base/chat_timeout/chat_enable_thinking`，密钥 `chat_api_key` 只走环境。
* 注册表与发现：`registry_*` 解析缓存、离线、私有、镜像、地址覆盖与覆盖有效期，`discovery_*` 发现开关、条数、缓存、离线，令牌 `discovery_github_token` 只走环境。
* 服务暴露：`mcp_host/mcp_port/mcp_path/mcp_enabled_tools/mcp_disabled_tools`，鉴权 `mcp_token/mcp_require_auth`（令牌只走环境），`web_host/web_port`。
* 治理：`snapshot_enabled/snapshot_dir/snapshot_quota_mb` 快照，`repos_dir/repos_quota_mb` 仓库配额，`deletion_log_ttl_days` 删除记录保留。
* 日志：`log_level` 取 `DEBUG/INFO/WARNING/ERROR`，`log_file/log_format` 取 `text/json`。
