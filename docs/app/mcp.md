# MCP 功能总览

MCP 出口面向模型，共二十九个工具。实现在 `src/libref/mcp_server/server.py`，组装时按配置过滤可见工具，未知工具名启动即报错。向量、重排与对话能力在组装处显式建立并复用于检索与问答。所有工具只做参数校验与服务调用，不抛协议层错误，失败以载荷内错误返回。写操作按 scope 校验，缺凭据返回未授权，缺 scope 返回禁止。

标准输入输出供本机客户端使用，`serve-mcp` 与 `serve --stdio` 共用同一组装路径，由命令直接注入本地平台主体并校验配置指纹；流式 HTTP 只由组合 `serve` 的 `/mcp` 提供，与 Web 共用 `[web]` 监听和鉴权中间件。dev 模式必须显式开启且只许回环；enforced 模式每请求需 `Authorization: Bearer`，浏览器来源另需在 `allowed_origins` 中精确登记。工具可见性由允许名单减去拒绝名单决定，缺省全部可见，空允许名单表示零工具。

错误信封统一为 `error/message`，错误名取统一错误表中的稳定码，例如 `invalid_argument`、`not_found`、`index_request_conflict`、`unavailable`、`forbidden`；注册表与索引申请的细粒度原因在归一化后归入这些稳定码。列表类返回条目加总数，检索与问答附带降级标记与原因，检索在空命中且要求建议时附带注册表建议。

## 检索问答

### search

预设检索，`preset` 三选一：`precise` 纯文本单路，`balanced` 融合召回（默认），`thorough` 在均衡上打开窗口扩展。`thorough` 只补邻块、不扩大召回，需要更广召回时调大 `limit`。数值类旋钮全部归配置，工具面只表达范围、条数与预设。

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| query | 是 | 无 | 查询词，非空 |
| library/version/ecosystem | 否 | 无 | 范围过滤 |
| limit | 否 | 8 | 1 到 100 的整数 |
| preset | 否 | balanced | `precise/balanced/thorough`，未知取值返回参数错误 |
| suggest | 否 | 关 | 空命中时跑一次模糊发现 |
| include_prerelease | 否 | 关 | 建议排序含预发布版本 |

返回 `items/count/degraded/reasons/answerable/answerability/cross_lingual`，`reasons` 为降级原因枚举（向量缺失除外：纯全文无向量不算降级）。`answerable` 为证据是否足以回答：查询与语料共享书写系统时按池级与头部词项覆盖判定，不共享脚本（如中文查询打英文语料）时记为 `weak` 而非拒答；`cross_lingual` 表示全文单路因书写系统不匹配而零命中、结果全部来自向量单路。结果不按该判定过滤，调用方自行决定是否弃答。`suggest` 且空命中时附带 `suggestions`，发现失败附带 `suggest_error`。

### graph_query

独立图查询，`mode` 五选一：`neighbors` 返回实体邻域，`path` 返回两实体间的有界最短路径，`usage` 返回邻域加频次、序列与实参形状，`community` 返回同簇兄弟实体与特征父链，`impact` 沿反向调用边列出影响面。`library` 与 `version` 必填且版本必须唯一，避免跨版本拼接；`profile` 绑定挖掘作用域；`depth` 受配置上限约束，关系类型可按 `relation_types` 过滤。返回节点、边、证据、截断标记与作用域，`usage` 另返回复用挖掘读模型的统计与文档节点。该工具不参与语义排序，也不改变检索结果分数。

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| mode | 否 | neighbors | `neighbors/path/usage/community/impact` |
| entity | 是 | 无 | 限定名或唯一后缀 |
| target | path 时必填 | 无 | 路径终点限定名 |
| library/version | 是 | 无 | 库与版本坐标 |
| ecosystem | 否 | 无 | 生态过滤 |
| profile | 否 | usage | 挖掘 profile 名称 |
| direction | 否 | both | `forward/backward/both`；`path` 按该方向约束边，`impact` 固定反向 |
| depth | 否 | 配置值 | 1 到配置上限的整数；`usage` 固定一跳，显式传入会返回参数错误 |
| relation_types | 否 | 无 | `call/import/inherit/mentions/covers_pattern/member/parent/doc_link/pattern_next` |
| include_ambiguous | 否 | 关 | 诊断时展开歧义与非目标解析边 |

### answer

预设检索后生成回答，返回回答文本、引用、降级标记与原因、模型名、置信度与引用编号。引用只保留回答实际引用的材料序号，无引用时引用表为空且置信度为低。`precise` 预设下不附带模式标签，保持零挖掘；`balanced/thorough` 在命中实体非空时附带至多三条不可引用的模式提示。材料总预算为硬上限，超出部分整条丢弃并以省略标记告知模型，引用编号与保留材料一一对应。对话能力缺配置时返回不可用原因。预设优先级为调用方显式指定高于预设高于配置；问答不展开窗口。

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| query | 是 | 无 | 查询词，非空 |
| library/version/ecosystem | 否 | 无 | 范围过滤 |
| limit | 否 | 5 | 1 到 50 的整数 |
| preset | 否 | balanced | `precise/balanced/thorough` |

## 阅读目录

### read_doc

按地址读取文档，`mode` 三选一：`page` 整页，`window` 按中心序号取前后邻块，`section` 按来源分组编号取整节。

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| url | 是 | 无 | 文档地址，非空 |
| library/version/ecosystem | 否 | 无 | 范围过滤 |
| mode | 否 | page | `page/window/section` |
| sort_order | window 必填 | 无 | 中心序号 |
| radius | 否 | 1 | 0 到 20 的整数，非整数或布尔值返回参数错误 |
| source_group_id | section 必填 | 无 | 来源分组编号，非空 |

地址无命中返回未找到。窗口与整节缺参返回参数错误。

### catalog

类目浏览，无库名返回库列表，给定库名返回该库版本列表。`library/ecosystem` 均可选。

## 发现解析

### library_search

按查询词跨注册表模糊发现候选。`query` 必填，`ecosystem` 可选，`limit` 取 1 到 100 默认 10，`include_prerelease` 可选。注册表失败返回其分类错误。

### web_search / github_search

显式搜索，结果未经核验，使用前需确认。`query` 必填，`limit` 取 1 到 100 默认 10。失败语义与模糊发现一致。

### resolve

按生态与名称解析文档或仓库地址。`name` 非空必填，`ecosystem/version` 可选，`target` 取 `docs/repo` 默认 `docs`，`include_prerelease` 可选。联网并更新注册表缓存，失败返回分类错误、提示与后续动作。

## 探查

### doc_probe

探查文档站结构，不存储。`start_url` 非空必填，`sample_limit` 取 1 到 100，`fetch_sample_limit` 取 0 到 100（0 表示禁用），`scope` 为对象（非对象返回参数错误），缺省跟随配置。`max_pages` 取 1 到 100000，超限不抛错而是在返回体中标记超预算并给出实际规模；`prefilter` 为布尔，打开时先做存活探测再发现。范围阈值与抓取同口径，命中查询词得分不足的目标同样过滤。返回体另带 `follow_links` 与跟随膨胀提示，开启跟随链接时实际抓取可能继续膨胀。

### repo_probe

返回的 `role_candidates` 只建议 test/example 角色并区分路径、注解、命名命中，不自动改变语料角色。

探查仓库目录结构，不解析。`source` 非空必填（远端地址；本地路径只在命令行可用，工具入口按参数错误拒绝），`subdir` 可选，`max_files` 取 1 到 100000，`sample_limit` 取 1 到 100，`only_files` 为非空字符串数组（单文件限定，先过滤再做预算判断），`exclude_patterns` 为非空字符串数组（排除匹配路径，与索引同口径），`tree_depth` 取 1 到 10（目录树展开深度，缺省 1），`samples_per_dir` 取 1 到 50（每目录采样上限，缺省关闭，总量受采样数约束）。返回体回显生效的限定与未命中的限定路径，附目录树（目录与递归文件数，最多 100 条）与按目录分组的采样。

### repo_dependencies

查询某版本清单声明的依赖。`library/version` 非空必填，`ecosystem/dep_ecosystem` 可选。

### link_stats

文档链接引用数统计，只读。`library/version` 非空必填，`ecosystem` 可选，`limit` 取 1 到 500，给出 `url` 时返回单页统计（出链数、站外出链数、被引用数、引用来源采样），不给时返回版本汇总加被引用排名。

## 源码洞察

范围过滤 `library/version/ecosystem` 在各视角中含义一致，缺省跨生态。`limit` 缺省沿用各视图默认值，显式给出时取 1 到 500。

### api_insight

接口视角：`usage` 频次榜，`related` 同现接口，`args` 参数形状，`common` 多接口公共调用。`related/args` 要求 `api`，`common` 要求 `api/apis` 之一。`apis` 为数组。

### pattern_insight

序列与结构视角：`patterns` 模式列表，`evidence` 模式证据，`tree` 特征树，`clusters` 分组，`health` 健康，`labels` 标签，`snippets` 片段，`facets` 形态，`feature_summary` 特征摘要，`cluster_labels` 分组标签，`drift` 漂移对比。带来源过滤的挖掘写入带指纹的独立档案，不改写默认 `usage`。`evidence` 要求非空 `pattern` 数组，`drift` 要求 `compare_profile`。`containing` 为必含接口数组，`prefix` 为点分前缀，`facet` 按维度过滤形态。`per_file_limit` 取 1 到 20。`dedup` 三态：真开关注入去重，假关闭，不给跟随配置。`profile` 选择挖掘档案，缺省为 `usage`。

### symbol_insight

符号视角：`detail` 要求 `api/apis` 之一，`file/children/signature` 要求 `api`。`offset` 默认为 0 的非负整数。

### call_insight

调用视角：`callee/caller/file` 均要求 `api`。`include_ambiguous` 计入待定调用，`resolve_kinds` 为解析来源数组，`relation_kinds` 为调用形态数组（显式指定可放行宏形态），`min_args/max_args` 为实参数量区间。`offset` 默认为 0。

### import_insight

导入视角：`importers/imports` 均要求 `api`。`offset` 默认为 0。

### mining_health

复用度与多样性信号，`library/version/ecosystem` 全部可选，直接返回健康对象。

### doc_insight

文档派生视角：`features` / `links` / `coverage` / `consistency`。`library/version` 必填，`ecosystem` 可选，`profile`、`facet`、`api`、`pattern`、`verdict`、`block_type`、`document_id` 与 `limit` 按视图过滤；`links` 行附带命中的特征树节点路径。

## 治理

### review

审核工作流。`action` 四选一：`list` 列待定项，`approve/conflict/reopen` 流转单实体。`entity_id` 在非列表动作下必填，`limit` 取 1 到 500 默认 50。非法流转返回冲突，缺参返回参数错误。

### job_show

查看任务。`job_id` 缺省列最近任务，`limit` 取 1 到 200 默认 20。未知编号返回参数错误。本工具只读，不提交任务，按调用方租户过滤。

### submit_feedback

提交开发者反馈，只记录不改审核状态。`body` 非空必填，`entity_type/entity_id/kind` 可选。需 `feedback:write` scope。

## 索引申请闭环

`request_repo_index` 与 `request_docs_index` 按坐标或 HTTP(S) 地址提交申请，返回请求编号；本地路径只允许本机 CLI 直连。仓库申请可带 `source_role` 与 `target_filter`，分别声明语料角色和目标 API 前缀或限定名。`index_request_status` 与 `wait_index_request` 按编号读取状态，后者有界轮询并在超时时返回当前状态而非抛错；`list_index_requests` 查看自己的申请，`review_index_request` 列表、通过或驳回待审队列。工具输出携带下一步工具名及终态后的读取工具建议。

申请与审批需 `index:request` / `index:approve`，状态与等待工具需 `read`。持有 `ingest:write` 的申请方直接入队；无该 scope 的审批者不能审批自己的申请。

## 可配置项

完整参数定义见同目录 `config.md`（真相源），以下为按功能归纳。组装与行为由配置决定，优先级为显式参数、环境变量、配置文件、内置默认。密钥只走环境变量。

* 可见性：`mcp_enabled_tools` 允许名单（缺省全量，空数组表示零工具），`mcp_disabled_tools` 拒绝名单（拒绝优先于允许）。未知工具名启动即报错。
* 传输与鉴权：`serve-mcp` 只走标准输入输出；组合 `serve` 的 `/mcp` 与 Web 共用 `web_host/web_port`，挂载路径取 `mcp_path`。`[auth]` 的 `auth_mode/dev_principal/allowed_hosts/allowed_origins` 决定 dev 与 enforced 行为，`bootstrap_admin_key` 只走环境；enforced 下只认 API Key，`librefctl` 的 Key 走调用方环境的 `LIBREF_CLIENT_API_KEY`。
* 检索问答：`vector_backend/simvec_dir/simvec_lib/qdrant_url/vector_collection/qdrant_timeout` 决定向量是否可用；`embedding_model/embedding_dim/embedding_api_base` 决定向量语义（密钥 `embedding_api_key` 只走环境）。`rerank_model/rerank_enabled/rerank_candidate_limit/rerank_top_n/rerank_truncate` 决定精排是否启用与窗口。`rrf_k/rrf_w_fts/rrf_w_vec`、`freq_alpha/facet_alpha`；派生关系与文档块特征只做结果增强，由 `doc_feature_annotation_top_n` 控制、`candidate_multiplier/assemble_multiplier/assemble_min`、`retrieval_max_versions`、`fts_w_content/fts_w_title/fts_w_url/fts_w_path`、`cluster_threshold/cluster_min_size`、`facet_topn/facet_candidate_budget/facet_min_gain/guard_enabled/drift_topn` 决定融合、加权与形态选择。`snippet_radius/snippet_cap`（前者须小于后者）、`window_preview_cap`、`retriever_window_radius/retriever_expand_window` 决定片段与窗口默认，详尽预设强制开窗口。`material_limit/answer_total_budget` 决定问答材料预算，`chat_model/chat_enabled/chat_api_base/chat_timeout/chat_enable_thinking` 决定生成（密钥 `chat_api_key` 只走环境）。
* 发现解析：`discovery_enabled/discovery_topn/discovery_cache_ttl_days/discovery_offline`，`registry_*` 解析缓存、离线、私有、镜像、地址覆盖与有效期，令牌 `discovery_github_token` 只走环境。
* 用法语料：`usage_mining_enabled/doc_code_mining_enabled` 控制源码文档注释与文档代码块是否进入 usage 档案。
* 洞察：`evidence_per_file_limit/evidence_dedup` 证据默认，`induction_enabled/induction_topn/induction_batch/induction_prompt_version` 标签归纳，`cluster_threshold/cluster_min_size` 分组默认，`facet_topn/facet_candidate_budget/facet_min_gain/guard_enabled` 形态选择，`drift_topn` 漂移截断。
* 日志：`log_level/log_file/log_format`。

`search` 支持 `block_type`、`exclude_deprecated`、`aligned_only` 过滤。
