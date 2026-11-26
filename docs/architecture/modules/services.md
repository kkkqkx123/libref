# services — 业务编排层

## 职责

三个出口（cli、mcp_server、web）唯一的调用面：向下编排 crawler / astminer / mining / chunking / store / db / llm，向上提供全部业务入口。出口不得绕过本层直接调 store/crawler/mining，也不得内联构造向量库或 LlmGateway——一律经 `factory.py` 组合根。

## 结构与关键文件

```
services/
├── factory.py        组合根：build_search/discovery/ingest/maintenance_service
├── infra/            jobs 队列、progress 断点、version_lock、tenant、quota、auth、vectors
├── code/             仓库索引与挖掘：code_ingest、repo_*、mining_refresh、mining_read、probe
├── docs/             文档摄取与检索：ingest、search、answer、feature_refresh、links、probe
├── ops/              维护（maintenance）、retention、review、feedback、audit、registry
```

## 功能清单

### code 侧 — 仓库索引管线（code_ingest.py 及 repo_*）

`index_repo_async` 主流程（先取 `version_lock` 版本锁）：

1. **预备**（repo_prepare）：解析源（显式路径或经 `registry.resolver.resolve_repo`）→ 注册使用语料源（source_key = `{source_role}:{sha1 指纹}`，可复现挖掘 profile）→ 确保库版本行 → 两级配额（磁盘目录 + repos_bytes 资源）→ `RepoManager.ensure` 克隆/更新（非 implementation 角色的 repo 名带 `/usage/{role}/{source_id}` 后缀；同名不同 URL 拒绝）→ 文件普查（`describe_source_files`，超 max_files 抛 BudgetExceeded）→ 可选 rustdoc 代码块摄取 → 批大小与断点游标。
2. **声明收集**（repo_collect）：逐文件先查 `ast_cache`（sha1+语法指纹命中则解码），否则 `extract_file_with_evidence` 解析并回写；收集 decls、import 别名、导出事实；原始调用留在缓存按批重载，峰值内存只与一批相关。
3. **分批解析持久化**（repo_resolve）：`resolve_calls` 解析调用 → upsert call_instances → code_search 时写源码 chunk → 向量同步后才 `advance_progress` 推进游标（**sync-then-mark**：崩溃后 resume 重放该批，游标永不越过未同步的向量）。
4. **收尾**：`_sweep_calls` 清消失的调用；清单身份校验与 `repo_dependencies` 写入；源码页面 sweep；最终 `sync_vectors` 校验；清除断点游标。

稳定标识：`entity_uid`/`call_uid`（语义键哈希）是 upsert 幂等键。持久化原语（repo_persist）负责实体 upsert + 版本内集合差 sweep（500/批）。

### code 侧 — 挖掘刷新（mining_refresh/）

- **共享调用集**（callset）：单次扫描 `in_target_api=1` 的调用，SQL 端过滤 resolve_kind/relation_kind/参数数，内存端叠加 review 状态与保留规则——conflict 一律剔除；macro 按 macro_policy（exclude/review_only/approved_only）；version_unconfirmed 与 AMBIGUOUS 需显式开关或 review approved。
- **`mine_patterns`**：校验参数（minsup≥2，2≤minlen≤maxlen≤10）→ `resolve_profile` 取 profile（指纹变更即清派生表）→ 按 `sample_id`（否则 scope_key）分组为序列（剔除 <2、截断 >1000）→ 归一化编码 → `PrefixSpan.frequent(minsup, closed=True)` 挖闭序列模式 → upsert `seq_patterns` + 集合差清理 → 联动刷新派生表。
- **派生刷新**（derived）：`refresh_feature_tree`（所有权链层级树）、`refresh_clusters`（并查集 Jaccard 聚类 → `api_clusters`）、`refresh_facets`（调用切面 → `feature_facets`）；`_refresh_doc_derived` 联动 docs 侧 `refresh_doc_features(scope="derived")` 并递归到绑定文档版本。
- **语义归纳**（induction）：对每个模式组装 `InductionInput`，调 chat 生成 `PatternLabel`，输出经材料边界标记黑名单与 allowed_apis 白名单校验，按 `(version_id, profile_id, pattern_hash, model, prompt_version)` 幂等 upsert `pattern_labels`。
- **作业化**：`build_mine_runner`/`build_induce_runner` 消费 `mine_patterns`/`induce_labels` 作业；`mine_job_params` 生成可重放参数快照。

### code 侧 — 读模型与探测

- **MiningReadService**（mining_read/，mixin 组合）：ApiUsageReader（调用计数/参数形状/相关 API）、PatternReader（模式列表/证据组装）、FeatureReader（特征树/聚类/切面）、SymbolReader（导入边/符号详情/调用者被调用者）、HealthReader（挖掘健康度、解析分布）、InsightReader（六类 insight 视图路由）。全部只读。
- **probe/probe_remote**：只读、零副作用的仓库探测（不解析文件、不写 repos 表）。远程四层最省优先降级：复用克隆（sha1 槽位 + TTL）→ 托管 API（GitHub/GitLab 列目录）→ blobless git 树（不下载 blob）→ 浅克隆；仅最后层抛错。复用与正式摄取相同的过滤函数，保证探测范围 = 作业执行范围。

### docs 侧 — 摄取（ingest.py）

`IngestService._scrape_async` 流程（先取版本锁）：

1. 种子发现（`UrlSeeder`）→ `plan_targets` 范围过滤 + 起始页兜底 + 分页截断（纯函数无 DB）。
2. 批处理每页 `PagePipeline.fetch_and_store`（ owns chunk 预算、清洗缓存、快照），`record_outcome` 汇总漏斗（stored/unchanged/absent/failed/…）。
3. 收集链接边（internal/external/structure）与 follow 队列；访问集与待爬队列持久化（断点续爬），超 max_pages 抛 BudgetExceeded。
4. **sync-then-mark**：批间向量同步完成后才保存游标与向量水印。
5. 收尾：写 `doc_link_edges`、sweep absent 页、最终向量校验同步、联动 `refresh_doc_features(scope="sources")`、记录站点版本证据、清游标。

`start_scrape_job`/`build_scrape_runner`/`run_scrape_job_sync` 把同一管线作业化（数据库认领队列 + 心跳）。

### docs 侧 — 检索与答案（search.py / answer.py / answer_materials.py）

- **预设路由**：`PRESETS = ("precise","balanced","thorough")`；`precise` 走纯 FTS 精确路径；`balanced/thorough` 走 `HybridRetriever` 融合；窗口扩展优先级为「调用方显式 > preset（thorough 强制展开）> 配置」。
- **答案链路**（answer_query_async）：检索取候选与状态 → 空结果或 evidence==none 时**弃答**（弱候选弃答即答案，追加 `low_evidence`）→ 取全文按排名适配总预算 → 附加挖掘模式标签提示（最多 3 条，precise 不加）→ 配额门（`llm_prompt_tokens`，cost≈chars/4）→ chat 生成 → `filter_citations_by_answer` 只保留答案真正引用的材料 → 返回 `{answer, citations, degraded, confidence}`。
- **接地材料**（answer_materials）：SYSTEM_PROMPT 强制只允许引用材料编号 [1][2]，pattern hints 是挖掘提示**绝不可被引用**，拒答需说明缺什么；`material_block` 渲染自定位 `unit_tag`（n/url/title/doc/chunk/page/order 属性），引用回放定位符齐全。

### docs 侧 — 特性派生与链接（feature_refresh.py / links.py）

`refresh_doc_features` 单事务按 scope（all/sources/derived 或单值）：

- structure：`aggregate_doc_facets` → `doc_features`。
- links：`align_documents`（文档↔API 实体对齐，min_confidence 默认 0.4）→ `doc_api_links` + `_backfill_entities`。
- coverage/consistency：`cover_patterns`（模式对文档的 example_code/prose 覆盖）→ `doc_pattern_coverage`；`compare_consistency`（漂移判定，可选 LLM 生成一致性说明）→ `doc_consistency`；`link_feature_nodes` 把对齐实体挂到特征树 → `doc_feature_node_links`。

`links.py`：`replace_version_edges`（write_lock 内 DELETE + INSERT OR IGNORE，边 kind 三态）与读视图（链接摘要/入链 top/单页统计）。

### infra 与 ops

- **JobManager**（infra/jobs）：数据库认领队列（submit 写入、worker 原子认领、heartbeat 续约）；五种作业 kind：scrape_docs / index_repo / mine_patterns / induce_labels / derive_doc_features。
- **progress**：断点游标体系（resume_offset/save_progress/vector_watermark/visited/pending），支撑 sync-then-mark 恢复语义。
- **version_lock / tenant / quota / auth / vectors**：跨写入方版本锁（stale_after 过期）；租户列可选探测；资源配额（repos_bytes、llm_prompt_tokens，拒绝记 `quota_denied_total` 指标）；向量栈全链路可选的构建工厂。
- **ops**：maintenance（reembed、FTS rebuild/check、reconcile、向量状态与优化）、retention（purge、删除日志 TTL）、review、feedback、registry 解析（resolver，镜像与 TTL 缓存）。

## 依赖与约束

向下可调 store/crawler/mining/chunking/db/astminer/llm；出口只能经 factory 组合根。内部 code/docs/ops/infra 允许互相组合但收敛在本层内（如 mining_refresh 联动 docs 的 feature_refresh）。跨子域正交调用必须走 services 内部函数，不暴露 SQL 给出口。
