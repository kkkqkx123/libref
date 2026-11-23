# 库文档特征挖掘实现细化方案

依据：`docs/ref/libref库文档特征挖掘补充设计.md`（下称专项设计）。本文把其 D1～D4 与 P0～P2 方向落实为当前代码可执行的模块、文件、行为与验收改动。全文为实施说明，不在代码中引入任何文档结构编号。

## 一、目标与边界

- 目标：把官方文档从“可检索文本”升格为“带结构特征、与 API 实体及社区模式对齐、可度量漂移”的知识层；最高优先级是修复 `documents.metadata.entities` 为空导致的块级实体断裂。
- 只做文档侧特征与文档-代码对齐；不在文档上重做 PrefixSpan 序列挖掘；不替换双路召回与 RRF 融合骨架；不引入重型依赖。
- 真源保持为 `pages` / `documents` / FTS、`api_entities`、`call_instances`、`seq_patterns`；新增四表全部为可重建派生数据，按版本与（需要时）挖掘档案隔离。
- 不做兼容层、不保留旧口径、不升级产品版本；`src/libref/config.py` 中同名配置直接扩展，不建立“新配置模块”。
- 代码注释只描述意图，不引用本文或专项设计的章节编号。

## 二、现状校准（决定实现口径的关键事实）

专项设计中的部分现状描述需要按当前代码修正，实施时以本节为准：

1. 迁移号不是 13。`src/libref/db/migrations.py` 已到 30（`cluster-labels`），本次新增 31 `doc-feature-facets`；`src/libref/db/postgres_schema.py` 是合并后的终态 DDL，必须在同一改动中同步。
2. 实体名单的写入链路已经存在但从未填充：`chunking/splitter.py:Chunk.entities` 默认空，`services/docs/scrape_plan.py:build_doc_inputs` 未透传，`store/document_store.py:store_page` 已把它写进 `documents.metadata.entities`，`store/retriever.py`、`services/docs/search.py` 也已读取。因此 D3 的重点是“产出并回写名单”，不是新建字段。
3. 文档示例代码已走 AST：`services/docs/page_pipeline.py:fetch_and_store` 抽取围栏或 HTML 代码块后调用 `services/code/usage_ingest.py:ingest_snippets`，产出 `source_role='doc_snippet'`、`source_key='doc:{version_id}:{url}'`、`caller_file=url`、`sample_id` 带片段序号的 `call_instances`，且带 `callee_entity_id`、`resolve_kind`、`confidence`。D3 必须复用这些证据，不应再解析一遍示例代码。
4. 挖掘侧已有档案、审核过滤、形态选择与健康门控：`services/code/usage_sources.py:resolve_profile`、`services/code/mining_refresh.py:_load_kept_calls`、`mining/scoring.py:capped_alpha`、`mining/facets.py`。文档侧加权与选择沿用同一组语义。
5. 检索已存在 `freq_alpha` / `cluster_alpha` / `facet_alpha` 三个乘法加成通道，新增 `doc_feature_alpha` 作为第四项，位置在 `store/retriever.py:HybridRetriever._assemble`。
6. 文档链编排在 `services/docs/ingest.py` 与 `services/docs/page_pipeline.py`，派生阶段必须落在 services 层；`cli` / `mcp_server` / `web` 只透传参数与格式化。

## 三、总体结构与数据流

新增纯算法模块，零数据库依赖：

- `src/libref/chunking/doc_facets.py`：D1 结构特征与 D2 内容特征的纯规则抽取；输入普通文档块数据，输出候选 facet 行；只依赖 `text/` 中的标识与分词工具，不导入 `db`、`services`、`config`。
- `src/libref/mining/doc_alignment.py`：D3 实体/模式对齐与 D4 差集判定的纯算法；输入普通字典与集合，输出 link / coverage / verdict 候选；复用 `mining/scoring.py` 的名称命中、序列包含与健康函数，不导入文档抓取或 AST 模块。
- 两个纯模块之间不互相导入；它们的输入适配由 services 完成，保持 `crawler`/`astminer` 分支独立。

服务层：

- `src/libref/services/docs/feature_refresh.py`：唯一写入口。加载文档块、实体、文档片段调用证据与档案模式，调用两个纯模块，批量幂等写四表，回写 `documents.metadata.entities`，做集合差清理与计数。
- `src/libref/services/docs/doc_insight.py`：唯一读模型。四视图读取、版本漂移对比、缺表降级为空。
- `src/libref/services/docs/feature_jobs.py`：任务化封装，注册新任务种类 `DOC_FEATURE_KIND = "derive_doc_features"`（常量本体放 `services/infra/jobs.py`，与 `MINE_PATTERNS_KIND` 同层），提供 runner 供 CLI 复用。

数据流：

```text
分块后的 documents 行
  → services/docs/feature_refresh 读取块元数据与现有 doc_snippet 调用证据
  → chunking/doc_facets（D1/D2，纯内存）
  → mining/doc_alignment（D3/D4，纯内存）
  → 四表批量写入 + documents.entities 回写
  → store/retriever 加权与过滤；services/docs/doc_insight 出口读取
```

## 四、数据模型与迁移

### 4.1 迁移 31

在 `src/libref/db/schema.py` 增加 `DOC_FEATURE_SQL`，在 `src/libref/db/migrations.py` 注册 `Migration(31, "doc-feature-facets", DOC_FEATURE_SQL)`；在 `src/libref/db/postgres_schema.py` 增加同结构表（`BIGINT` 主键外键风格与现有表一致）。四表均带 `library_id`、`version_id`、`tenant_id`，外键指向 `documents(id)` / `api_entities(id)` 并设置级联删除；`profile_id` 为普通整数列，与现有挖掘派生表一致。

| 表 | 唯一键 | 主要附加列 | 隔离口径 |
|---|---|---|---|
| doc_features | (version_id, document_id, facet) | value JSON | 版本级；D1/D2 只依赖文档正文 |
| doc_api_links | (version_id, document_id, api_entity_id) | match_kind、confidence、evidence JSON | 版本级；符号对齐只依赖实体事实 |
| doc_pattern_coverage | (profile_id, document_id, pattern_hash, covered_kind) | pattern JSON、support | 档案级；依赖 `seq_patterns` |
| doc_consistency | (profile_id, version_id, document_id, facet, model, prompt_version) | verdict、note、evidence_hash、updated_at | 档案级；D4 结论可重建 |

约定细化：

- `match_kind` 严格取 `symbol` / `signature` / `name_fuzzy`；AST 精确证据归入 `symbol`，由 `evidence` 记录 `sample_id`、来源角色与命中片段哈希。
- `covered_kind` 取 `example_code` / `prose`；模式身份用规范化模式 JSON 的哈希，不引用重建后可变的 `seq_patterns.id`。
- `verdict` 取 `match` / `drift` / `unknown`；确定性规则结果的 `model` 写固定规则标识，LLM 归纳结果的 `model`、`prompt_version`、`evidence_hash` 必须非空，重复归纳按幂等键命中即跳过。
- 索引：`doc_features(version_id, facet)`；`doc_api_links(version_id, api_entity_id)`；`doc_pattern_coverage(profile_id, version_id, covered_kind)`；`doc_consistency(profile_id, version_id, verdict)`。
- 清理顺序：`doc_consistency` → `doc_pattern_coverage` → `doc_api_links` → `doc_features` → `pages` 级联；按版本清理时同时带 `profile_id`（对档案级表），版本清除在 `services/ops/retention.py:_purge_inner` 中按上述顺序补齐四表。
- 审核冲突行不进入任何派生表；`review_status` 的过滤沿用 `mining_refresh._load_kept_calls` 口径，在 `feature_refresh` 内统一实现一次，不复制多份判断。

### 4.2 与专项设计草案的差异说明

专项设计草案把四表都写成版本键且迁移号为 13；当前代码已有多档案与多租户体系，因此本次只对依赖挖掘模式的表加 `profile_id`，避免把与档案无关的 D1/D2 特征重复写多份；迁移号按当前链条顺延。该差异不改变专项设计的清除顺序、派生属性与读模型边界。

## 五、阶段一：D3 对齐（最高优先级）

### 5.1 输入归一化

在 `services/code/usage_ingest.py` 提取 `doc_source_key(version_id, url)`，供 `page_pipeline` 与 `feature_refresh` 共用，避免源键拼写漂移。`feature_refresh` 按 `code_sources.source_key`、`source_role='doc_snippet'` 找到 `source_id`，再取对应 `call_instances`；连接文档用 `documents.url = code_sources.source_url`（等价于 `call_instances.caller_file`），并限定同版本。

调用证据过滤：排除 `review_status='conflict'`、`confidence='AMBIGUOUS'`、`target_status` 不在当前/已确认集合的行；保留 `callee_entity_id` 非空的行作为最强证据。`sample_id` 中的片段序号与 D2 计算出的片段哈希共同决定命中哪一个 `document_id`；哈希不匹配时退化为“同页 + 限定名在块正文中出现”的弱证据，并降低置信度，不猜测多条边。

### 5.2 纯算法契约

`mining/doc_alignment.py` 暴露三类纯函数：

- `build_api_index(entities)`：按版本实体构造限定名、短名、.snake 归一形式的倒排索引；同名多实体视为歧义，直接不参与模糊匹配。
- `align_documents(blocks, api_index, snippet_calls)`：按优先级产出 link 候选。优先级为：已解析 AST 调用（`symbol`，confidence 1.0）→ 签名/限定名的词边界命中（`signature`，0.8）→ 短名归一后唯一命中（`name_fuzzy`，0.4）。低于配置阈值的候选丢弃；每块上限按配置截断，截断计数返回给编排层。
- `cover_patterns(blocks, patterns)`：把 `seq_patterns` 的规范化序列与文档示例调用序列做保序包含判定（与 PrefixSpan 的子序列语义一致），产出 `example_code` / `prose` 覆盖候选；纯文本叙述只在限定名完整命中时记 `prose`，不把自然段当调用序列。

### 5.3 编排与回写

`feature_refresh.refresh_doc_features(conn, library, version, ...)` 的 D3 部分：

1. 只处理当前版本 `documents` 行；按 URL 分组，加载对应代码源与调用证据，调用纯算法。
2. 批量 upsert `doc_api_links`，读取存活键内存求差后按批删除失效边（每批五百，空集整版清空）。
3. 回写 `documents.metadata.entities`：按 `document_id` 聚合已落库边的限定名，与现有列表比较，不同才更新 `metadata`，避免触发 FTS 触发器与无谓的行变更；实体为空时写空列表，不留旧值。
4. 同一事务内提交；失败整体回滚，调用方只记告警计数，不改变已成功的源文档真源。
5. 模式覆盖按 `resolve_profile` 得到的 `profile_id` 重建；`mine_patterns` 成功后调用同一刷新入口的覆盖部分（`profile_id` 显式传入），避免模式已更新而文档覆盖仍是旧值。

### 5.4 触发方式

- `IngestService._scrape_async` 在源写入与向量同步之后、按版本串行锁释放之前调用 D1～D3 刷新（由 `doc_feature_enabled` 控制）；失败计数不推翻抓取结果。
- CLI 新增 `doc-features refresh`，支持 `--library --version --ecosystem --profile --scope {structure,links,coverage,consistency,all} --tenant`；`--scope all` 为默认全量重建，任务化提交走新的 `DOC_FEATURE_KIND`。
- 已存在的 `mine_patterns` 命令在模式重建后增量触发 `coverage`，不重复 D1/D2。

### 5.5 验收

- golden 集（阶段四建设）中“应关联的 API 实体”准确率 ≥ 0.9；文档块 `entities` 非空率相对当前基线显著提升。
- 同一版本重跑两次结果完全一致；删除一个页面后对应边与实体名单被清除；审核冲突调用不产生边。
- 只调用纯算法时不需要数据库连接。

## 六、阶段二：D1/D2 结构与内容特征

### 6.1 纯算法

`chunking/doc_facets.py` 输入普通块数据（标题路径、正文、块路径、字符区间、围栏片段列表），输出 facet 行。 facets 固定枚举，不按随机语料自生长：

| 层 | facet | 取值与判定 |
|---|---|---|
| D1 | block_type | `narrative` / `example` / `params` / `warning` / `changelog`；按标题路径、围栏、表格与提示词优先级判定，优先级固定为 changelog → warning → params → example → narrative |
| D1 | heading_depth | 标题路径段数分桶 |
| D1 | length_bucket | 正文长度分桶，阈值来自配置 |
| D1 | code_ratio | 围栏字符数占正文字符数，截断到 0～1 |
| D1 | has_table | Markdown 表格行或 HTML 表格残留标记 |
| D1 | deprecated | `deprecated` / `废弃` / `不再支持` / `removed in` 等标记 |
| D1 | version_marker | 语义版本样式片段的限量列表 |
| D2 | example_language | 围栏语言标签（规范化到语言配置名） |
| D2 | example_symbols | 示例片段中的调用限定名列表，来自 D3 已落库的 AST 证据，纯模块只接收普通字符串集合 |
| D2 | param_names | 参数表首列或 `参数` 小节中的标识符候选 |
| D2 | return_hint | 返回值描述小节的标识符候选 |
| D2 | term_candidates | 标题与正文高频标识符，按 `text/ident.py` 归一后限量 |

围栏识别与 `chunking/splitter.py` 的围栏保护口径一致，但不复用其 `_FENCE_RE` 私有实现：在 `doc_facets.py` 内定义同形制的公开规则，避免跨模块引用私有符号；块内容中围栏必须成对，否则不出 `example` 判定。

### 6.2 候选控制（expand-and-reduce）

- expand 集合就是上表的固定枚举；每个 facet 的取值条数受 `doc_feature_terms_per_facet` 限制，单块 facet 总数受 `doc_feature_candidate_budget` 限制。
- reduce 不依赖运行时启发式打分：`doc_feature_enabled_facets` 是配置化的启用清单，清单外的 facet 不计算也不落库；清单由阶段四 golden/判定集在离线评测后确定。这样避免“检索增益”在构建期不可得却写猜测值。
- 超预算时按 facet 优先级整批丢弃并计数（`doc_features_dropped`），计数进入刷新结果与日志，不做静默截尾。

### 6.3 编排与验收

- `feature_refresh` 先调用 `doc_facets.aggregate(blocks)`，再调用 D3，因为 `example_symbols` 需要 AST 证据回填；纯模块本身保持无状态。
- `doc_features` 的 value JSON 结构固定为 `kind`、`details` 两部分；同一 facet 每个文档一行，字段增删直接改口径并重跑全量刷新，不读旧格式。
- 验收：块类型抽检一致率 ≥ 0.85；围栏跨块时两个块都能被标为 `example` 且不会二次切分正文；候选数始终不超预算；关闭全部 facet 时四表写入为零但抓取与检索主路径不受影响。

## 七、阶段三：D4 一致性与漂移

### 7.1 确定性判定

`mining/doc_alignment.py:compare_consistency(links, coverage, deprecated, pattern_support)` 按以下规则产出 `verdict`，不做模型推理：

- `match`：文档示例调用序列覆盖了当前档案支持度达标的模式，或实体边与社区高频实体集合一致。
- `drift`：文档示例包含已标记废弃的实体，或其限定名落在本版本已有模式涉及实体内却没有任何覆盖；支持度阈值由 `doc_consistency_min_support` 给出，避免低频模式制造噪声。
- `unknown`：样本不足、无代码源、无对应模式或证据被审核过滤；不写猜测结论。

`doc_consistency.facet` 固定为 `pattern_alignment` 与 `deprecated_usage` 两类；`evidence_hash` 由参与判定的 link、coverage 与废弃标记集合排序后计算，保证同输入同结论。

### 7.2 LLM 归纳层

- 仅对 `drift` 样本汇总后批量提交，复用 `mining/induction.py` 的输出校验与 `llm/gateway.py` 的共享韧性状态；归纳只写 `note`，不得改写 `verdict`。
- 默认关闭（`doc_consistency_llm_enabled=false`）；开启时按 `doc_consistency_batch` 分批、按 `induction_prompt_version` 同款方式记录提示版本，失败只计数告警并保留确定性结论。
- `doc_consistency` 是派生表，按版本/档案整版清除；模型产物不进入真源，不参与问答引用定位。

### 7.3 跨版本对比

`services/docs/doc_insight.py` 提供 `compare_versions`：按 URL 与 facet 连接两个版本的派生行，产出新增、消失、支持度显著变化三类清单，条数受 `drift_topn` 限制；只做读时计算，不新增存储表。相同档案自比对必须返回空清单。

### 7.4 验收

- drift 样本人工抽检误报率 < 20% 才允许默认开启出口展示。
- LLM 关闭时行为与阶段二完全一致；开启后重复运行结果稳定；禁用模式时全部落 `unknown`。

## 八、检索接入与出口

### 8.1 加权

- `config.py` 新增 `doc_feature_alpha: float = 0.0`（`retrieval` 分组），默认关闭；同步 `libref.example.toml`、`docs/app/config.md`、`docs/app/cli.md`、`docs/app/mcp.md`、`docs/app/web.md` 的配置说明。
- `store/retriever.py:_assemble` 在 `info['id']` 维度加载当前版本 `doc_features` 的对齐与示例 facet，按块级计算加成：有实体对齐且含示例代码取正向值，废弃标记取负向值；复用 `capped_alpha` 的健康门控，仅在 `doc_feature_alpha > 0` 时执行额外读取。最终乘法项追加到既有三项之后，不修改 RRF 与双路召回。
- `services/docs/answer_materials.py` 与 `services/docs/answer.py` 不因加权改变引用编号规则；`entities` 名单仅作展示与命中解释，不新增生成内容。

### 8.2 过滤与排序

- `SearchService.search_with_mode`、`HybridRetriever.search_async` 新增显式参数 `block_type: str | None`、`exclude_deprecated: bool`、`aligned_only: bool`；在 FTS 与向量融合去重后、精排与组装前按 `doc_features` / `doc_api_links` 过滤；过滤激活时召回预算按 `candidate_multiplier` 放大，保证结果数不无故缩水。
- `SearchService.search_docs`（精确路）做同样过滤，放在 alignment 去重之后、`[:limit]` 之前。
- 无效值（未知块类型、互斥过滤组合）在 services 层按既有 `invalid_argument` 口径抛出，出口只负责透传。

### 8.3 四个出口

| 出口 | 改动 |
|---|---|
| MCP | 新增 `doc_insight` 工具，视图为 `features` / `links` / `coverage` / `consistency`；参数含库、版本、生态、档案、视图、实体名、模式哈希、条数；缺表返回空列表；工具注册与工具清单同时更新，`mcp_server/contract.py` 由签名生成 schema，无需另写校验 |
| CLI | `cli/parsers.py` 新增 `doc-insight` 与 `doc-features refresh`；`cli/docs_commands.py` 只做参数透传和表格/JSON 格式化；`search` / `answer` 增加过滤参数 |
| Web | `web/api.py` 新增洞察请求与响应模型，路由只调 `services/docs/doc_insight.py`；网页增加洞察页与块类型、废弃、对齐过滤控件；前端类型定义随响应模型同步 |
| 问答 | 详尽预设（`thorough`）打开 D3 加成与块类型过滤的显式开关，精确预设保持原行为；问答材料预算与引用编号不变 |

## 九、测试与评测

### 9.1 单元测试（无数据库）

- `tests/unit/test_doc_facets.py`：围栏成对、表格、废弃标记、长度分桶、候选预算、超预算计数。
- `tests/unit/test_doc_alignment.py`：四类实体的匹配优先级、同名歧义丢弃、弱证据降级、子序列覆盖、废弃判定与 `unknown` 条件。
- `tests/unit/test_doc_feature_scoring.py`：加成与过滤helper 的边界（无实体、无 facet、无档案、健康门控为零）。

### 9.2 集成测试（SQLite / 出口）

- `tests/integration/test_doc_feature_refresh.py`：迁移 31 生效；两后端 schema 对齐用例同步；D1～D3 全量重跑幂等；代码源删除或页面删除后边与实体名单被清除；审核冲突不入表；FTS 触发器在实体回写后仍可查询；`documents.metadata` 未变化时避免无谓更新。
- `tests/integration/test_doc_feature_retrieval.py`：过滤后的结果数、精确路与混合路一致性、`doc_feature_alpha=0` 与改造前排序一致、开启后加成方向正确。
- `tests/integration/test_doc_insight_outputs.py`：MCP / CLI / Web 三出口同参同果、四视图键稳定、缺表降级为空。
- 设置一律通过 `tests/conftest.py` 的 `make_settings` 与现有预设构造；新子系统若仅单文件使用，配置在用例内内联，不新增预设。

### 9.3 黄金集与 bench

- `tests/fixtures/doc_features/` 固定小规模标注：每库 30～50 条查询，标注“应命中的块”和“应关联的实体”，不标注模型偏好；待审与冲突证据不进入真值。
- `docs/bench/design.md` 补“文档侧加权判定集”：查询只命中一个实体、正确答案依赖示例块与对齐边的页面；在该判定集上度量 D3 准确率与 nDCG 增益，未标定前 `doc_feature_alpha` 保持 0。
- 差异用例：固定输入下一次口径变更（例如匹配优先级或 facet 集合）必须被断言捕获；同一档案两次刷新结果完全一致。

## 十、文档动作

- 新增 `docs/features/doc-feature-mining.md`，作为文档侧特征唯一功能说明，写明 D1～D4、四表、刷新顺序、加权与过滤语义；专项设计只作参考依据，不再另维护第二份功能口径。
- `docs/features/db/derived.md` 增补四表结构、写入与读取口径，并把开头“六表”改为实际总数。
- `docs/architecture/doc-indexing-chain.md` 在“落库与向量同步”“检索语义”两节补派生阶段、实体回写与第四加权项，保持“真源/派生”区分。
- `docs/bench/design.md` 补文档侧判定集要求；`docs/features/feature-mining.md` 增加指向功能篇的链接，避免两处重复定义。
- 配置说明按第八节列出的文件同步更新；版本与依赖文件保持不变。

## 十一、实施顺序与提交粒度

1. 迁移 31 与四表、两后端同步、清除顺序；先落 schema 测试。
2. D3：纯匹配模块、刷新编排、实体回写、golden 集；先验证溯源准确率，再谈加权。
3. D1/D2：结构/内容 facet、预算与启用清单、块类型过滤。
4. D4：确定性漂移、可选 LLM 归纳、跨版本读时对比。
5. 检索第四加权项与 `doc_feature_alpha` 判定集；未标定前默认 0，仅保留加权通道与测试。
6. MCP / CLI / Web 四视图与文档同步。

每个提交前统一运行测试、lint、格式与类型检查；输出过长时先重定向到文件再过滤查看。凡改动检索或挖掘逻辑，必须同时提交 golden 或差异用例。
