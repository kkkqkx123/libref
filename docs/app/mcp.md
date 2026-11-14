# MCP 功能总览

MCP 出口是面向模型的只读问答面，共十九个工具。实现在 `src/libref/mcp_server/server.py`，组装时按配置过滤可见工具，未知工具名启动即报错。向量、重排与对话能力在组装处显式建立并复用于检索与问答。所有工具只做参数校验与服务调用，不抛协议层错误，失败以载荷内错误返回。

传输支持标准输入输出与流式 HTTP 两种形态。流式形态默认绑定回环，非回环监听且无令牌时启动拒绝。工具可见性由允许名单减去拒绝名单决定，缺省全部可见，空允许名单表示零工具。

错误信封统一为 `error/message`，错误名取 `invalid/not_found/conflict/unavailable/failed` 与注册表分类错误。列表类返回条目加总数，检索与问答附带降级标记与原因，检索在空命中且要求建议时附带注册表建议。

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

探查文档站结构，不存储。`start_url` 非空必填，`sample_limit` 取 1 到 100，`fetch_sample_limit` 取 0 到 100（0 表示禁用），`scope` 为对象（非对象返回参数错误），缺省跟随配置。

### repo_probe

探查仓库目录结构，不解析。`source` 非空必填（远端地址或本地路径），`subdir` 可选，`max_files` 取 1 到 100000，`sample_limit` 取 1 到 100。

### repo_dependencies

查询某版本清单声明的依赖。`library/version` 非空必填，`ecosystem/dep_ecosystem` 可选。

## 源码洞察

范围过滤 `library/version/ecosystem` 在各视角中含义一致，缺省跨生态。`limit` 缺省沿用各视图默认值，显式给出时取 1 到 500。

### api_insight

接口视角：`usage` 频次榜，`related` 同现接口，`args` 参数形状，`common` 多接口公共调用。`related/args` 要求 `api`，`common` 要求 `api/apis` 之一。`apis` 为数组。

### pattern_insight

序列与结构视角：`patterns` 模式列表，`evidence` 模式证据，`tree` 特征树，`clusters` 分组，`health` 健康，`labels` 标签，`snippets` 片段。`evidence` 要求非空 `pattern` 数组。`containing` 为必含接口数组，`prefix` 为点分前缀。`per_file_limit` 取 1 到 20。`dedup` 三态：真开关注入去重，假关闭，不给跟随配置。

### symbol_insight

符号视角：`detail` 要求 `api/apis` 之一，`file/children/signature` 要求 `api`。`offset` 默认为 0 的非负整数。

### call_insight

调用视角：`callee/caller/file` 均要求 `api`。`include_ambiguous` 计入待定调用，`resolve_kinds` 为调用种类数组，`min_args/max_args` 为实参数量区间。`offset` 默认为 0。

### import_insight

导入视角：`importers/imports` 均要求 `api`。`offset` 默认为 0。

### mining_health

复用度与多样性信号，`library/version/ecosystem` 全部可选，直接返回健康对象。

## 治理

### review

审核工作流。`action` 四选一：`list` 列待定项，`approve/conflict/reopen` 流转单实体。`entity_id` 在非列表动作下必填，`limit` 取 1 到 500 默认 50。非法流转返回冲突，缺参返回参数错误。

### job_show

查看任务。`job_id` 缺省列最近任务，`limit` 取 1 到 200 默认 20。未知编号返回参数错误。本工具只读，不提交任务。

## 可配置项

完整参数定义见同目录 `config.md`（真相源），以下为按功能归纳。组装与行为由配置决定，优先级为显式参数、环境变量、配置文件、内置默认。密钥只走环境变量。

* 可见性：`mcp_enabled_tools` 允许名单（缺省全量，空数组表示零工具），`mcp_disabled_tools` 拒绝名单（拒绝优先于允许）。未知工具名启动即报错。
* 传输：`mcp_host/mcp_port/mcp_path`，鉴权 `mcp_token/mcp_require_auth`（令牌只走环境；非回环监听无令牌时启动拒绝）。
* 检索问答：`vector_backend/simvec_dir/simvec_lib/qdrant_url/vector_collection/qdrant_timeout` 决定向量是否可用；`embedding_model/embedding_dim/embedding_api_base` 决定向量语义（密钥 `embedding_api_key` 只走环境）。`rerank_model/rerank_enabled/rerank_candidate_limit/rerank_top_n/rerank_truncate` 决定精排是否启用与窗口。`rrf_k/rrf_w_fts/rrf_w_vec`、`freq_alpha/cluster_alpha`、`candidate_multiplier/assemble_multiplier/assemble_min`、`retrieval_max_versions`、`fts_w_content/fts_w_title/fts_w_url/fts_w_path`、`cluster_threshold/cluster_min_size` 决定融合与加权。`snippet_radius/snippet_cap`（前者须小于后者）、`window_preview_cap`、`retriever_window_radius/retriever_expand_window` 决定片段与窗口默认，详尽预设强制开窗口。`material_limit/answer_total_budget` 决定问答材料预算，`chat_model/chat_enabled/chat_api_base/chat_timeout/chat_enable_thinking` 决定生成（密钥 `chat_api_key` 只走环境）。
* 发现解析：`discovery_enabled/discovery_topn/discovery_cache_ttl_days/discovery_offline`，`registry_*` 解析缓存、离线、私有、镜像、地址覆盖与有效期，令牌 `discovery_github_token` 只走环境。
* 洞察：`evidence_per_file_limit/evidence_dedup` 证据默认，`induction_enabled/induction_topn/induction_batch/induction_prompt_version` 标签归纳，`cluster_threshold/cluster_min_size` 分组默认。
* 日志：`log_level/log_file/log_format`。
