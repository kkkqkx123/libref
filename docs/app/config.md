# 参数设置总览

本文件是全部可配置项的真相源，与 `src/libref/config.py` 的 `LibrefSettings` 字段一一对应。字段增删改时须同步更新本文档。功能视角的说明见同目录的 `cli.md`、`mcp.md`、`web.md`，数值定义以本文为准。

## 生效规则

* 优先级：显式参数（命令行选项与调用方覆盖）高于环境变量（含本地文件注入），环境变量高于配置文件，配置文件高于内置默认。快照一旦生成即冻结为只读。
* 环境变量：字段名前加 `LIBREF_` 前缀，如 `db_path` 对应 `LIBREF_DB_PATH`，另支持同目录 `.env` 文件注入。
* 配置文件：`libref.toml`，按显式路径、当前目录、用户配置目录顺序查找。分节写法的节名与键名见各表“文件键”列（如 `[retrieval] rrf_k`）；与字段同名的顶层键同样接受。未知节与未知键直接报错。
* 密钥：`embedding_api_key/chat_api_key/discovery_github_token/bootstrap_admin_key/postgres_url/qdrant_api_key` 只走环境变量，写入文件即报错。`config-show` 打印脱敏后的有效配置，已设置的密钥显示为掩码。
* 路径：`db_path/registry_overrides_file/snapshot_dir/repos_dir/log_file/simvec_dir` 支持 `~` 展开；文件中为相对路径时相对配置文件所在目录，运行时传入的相对路径相对当前工作目录。
* 校验：未知覆盖键报错；`log_level` 仅接受 `DEBUG/INFO/WARNING/ERROR`，`log_format` 仅接受 `text/json`；`snippet_cap` 须大于 `snippet_radius`；`rerank_top_n` 不得超过 `rerank_candidate_limit`；`chunk_embed_min_tokens` 不得超过 `chunk_embed_max_tokens`；`chunk_bm25_min_terms` 不得超过 `chunk_bm25_max_terms`；`answer_weak_threshold` 不得超过 `answer_strong_threshold`；HNSW 构造 ef 不得低于 `max(2*m, 4)`；`qdrant_write_consistency_factor` 不得超过 `qdrant_replication_factor`；选用 `qdrant` 后端时 `qdrant_url` 必填，选用 `postgres` 后端时 `postgres_url` 必填。数值字段的上下界见各表。
* 指纹：有效快照的哈希用于核对多端配置是否一致，`config-show` 与 Web 健康检查均可查看。

## 服务与运行（[server]）

| 字段 | 类型 | 默认 | 环境变量 | 影响 |
|---|---|---|---|---|
| db_path | 路径 | `~/.local/share/libref/libref.db` | LIBREF_DB_PATH | 本机控制面 SQLite 位置与派生索引命名依据 |
| heartbeat_timeout | 浮点（>0） | 600.0 | LIBREF_HEARTBEAT_TIMEOUT | 任务心跳与版本锁的过期阈值，秒 |
| deployment_id | 字符串 | 空 | LIBREF_DEPLOYMENT_ID | 共享向量服务的部署标识；空时回退到 `instance_id` |

## 抓取（[crawler]）

| 字段 | 类型 | 默认 | 环境变量 | 影响 |
|---|---|---|---|---|
| fetch_concurrency | 整数（≥1） | 4 | LIBREF_FETCH_CONCURRENCY | 抓取并发数 |
| request_timeout | 浮点（>0） | 30.0 | LIBREF_REQUEST_TIMEOUT | 抓取超时，秒 |
| fetch_max_retries | 整数（≥0） | 3 | LIBREF_FETCH_MAX_RETRIES | 抓取重试上限 |
| fetch_max_bytes | 整数（≥1） | 5242880 | LIBREF_FETCH_MAX_BYTES | 单页响应体上限，字节，超限记失败 |
| crawl_delay_default | 浮点（≥0） | 1.0 | LIBREF_CRAWL_DELAY_DEFAULT | 默认抓取间隔，秒 |
| robots_cache_ttl_days | 浮点（≥0） | 7.0 | LIBREF_ROBOTS_CACHE_TTL_DAYS | robots 缓存有效期，天 |
| user_agent | 字符串 | libref-crawler/0.1 | LIBREF_USER_AGENT | 抓取请求头标识 |
| http_proxy | 字符串/空 | 空 | LIBREF_HTTP_PROXY | 显式代理，优先于标准环境变量 |
| no_proxy | 字符串/空 | 空 | LIBREF_NO_PROXY | 代理白名单，优先于 NO_PROXY 环境变量 |
| render_js_default | 布尔 | 关 | LIBREF_RENDER_JS_DEFAULT | 疑似渲染页默认回退浏览器 |
| render_wait_for | 字符串/空 | 空 | LIBREF_RENDER_WAIT_FOR | 浏览器等待的选择器 |
| render_timeout_ms | 整数（>0） | 15000 | LIBREF_RENDER_TIMEOUT_MS | 浏览器超时，毫秒 |
| seeder_cache_ttl_hours | 浮点（≥0） | 0.0 | LIBREF_SEEDER_CACHE_TTL_HOURS | 种子缓存，0 表示禁用 |
| follow_links_default | 布尔 | 关 | LIBREF_FOLLOW_LINKS_DEFAULT | 默认跟随页内链接 |
| ingest_clean_cache_cap | 整数（≥1） | 512 | LIBREF_INGEST_CLEAN_CACHE_CAP | 入库清洗缓存上限 |
| ingest_batch_pages | 整数（≥1） | 200 | LIBREF_INGEST_BATCH_PAGES | 入库批处理页数 |
| index_batch_files | 整数（≥1） | 200 | LIBREF_INDEX_BATCH_FILES | 建库批处理文件数 |
| usage_mining_enabled | 布尔 | 开 | LIBREF_USAGE_MINING_ENABLED | 是否从源码文档注释与文档代码块抽取用法语料 |
| doc_code_mining_enabled | 布尔 | 开 | LIBREF_DOC_CODE_MINING_ENABLED | 文档抓取时是否抽取语言标注代码块 |
| stdlib_filter_enabled | 布尔 | 开 | LIBREF_STDLIB_FILTER_ENABLED | 抽取阶段是否把标准库调用标为 stdlib 并默认排除 |
| stdlib_table_version | 字符串 | 1 | LIBREF_STDLIB_TABLE_VERSION | 标准库语言表版本，变更时重新索引才能生效 |
| probe_sample_limit | 整数（≥1） | 20 | LIBREF_PROBE_SAMPLE_LIMIT | 探查采样上限 |
| probe_cache_ttl_hours | 浮点（≥0） | 1.0 | LIBREF_PROBE_CACHE_TTL_HOURS | 探查缓存有效期，小时 |
| probe_fetch_sample_limit | 整数（≥0） | 5 | LIBREF_PROBE_FETCH_SAMPLE_LIMIT | 探查拉取标题与类型的页数，0 表示禁用 |
| probe_fetch_timeout | 浮点（>0） | 10.0 | LIBREF_PROBE_FETCH_TIMEOUT | 探查单页超时，秒 |

相关命令：`libref smoke-local`、`libref local probe-repo` 与 `librefctl ingest-docs/doc-probe`。

## 存储（[storage]）

| 字段 | 类型 | 默认 | 环境变量 | 影响 |
|---|---|---|---|---|
| qdrant_url | 字符串 | http://localhost:6333 | LIBREF_QDRANT_URL | Qdrant 后端地址，仅 qdrant 模式使用 |
| vector_collection | 字符串 | libref_chunks | LIBREF_VECTOR_COLLECTION | 向量集合基名，实际按库文件与部署标识派生隔离 |
| qdrant_timeout | 浮点（>0） | 5.0 | LIBREF_QDRANT_TIMEOUT | Qdrant 超时，秒 |
| vector_backend | 字面量 | simvec | LIBREF_VECTOR_BACKEND | `simvec` 本地引擎或 `qdrant` 远端服务 |
| simvec_dir | 路径/空 | 空 | LIBREF_SIMVEC_DIR | 本地引擎目录，缺省按库文件派生 |
| simvec_lib | 字符串/空 | 空 | LIBREF_SIMVEC_LIB | 本地引擎动态库显式路径 |
| vector_distance | 字面量 | cosine | LIBREF_VECTOR_DISTANCE | 向量距离算法：`cosine/euclid/dot/manhattan` |
| vector_write_batch | 整数（≥1） | 512 | LIBREF_VECTOR_WRITE_BATCH | 两种后端共用的写入批大小 |
| vector_max_points | 整数（≥1）/空 | 空 | LIBREF_VECTOR_MAX_POINTS | 派生索引点数硬上限，超限写入失败且不截断真源 |
| qdrant_api_key | 密钥/空 | 空 | LIBREF_QDRANT_API_KEY | Qdrant API Key，设置时默认启用 HTTPS |
| qdrant_https | 布尔/空 | 空 | LIBREF_QDRANT_HTTPS | 显式切换 Qdrant HTTPS，空时按 Key 推导 |
| qdrant_search_hnsw_ef | 整数（≥1）/空 | 空 | LIBREF_QDRANT_SEARCH_HNSW_EF | 远端图搜索束宽，空时沿用服务端默认 |
| qdrant_search_exact | 布尔 | 关 | LIBREF_QDRANT_SEARCH_EXACT | 远端搜索强制全量比对 |
| simvec_index | 字面量 | hnsw | LIBREF_SIMVEC_INDEX | 本地引擎索引形态：`hnsw/flat` |
| simvec_hnsw_m | 整数（≥2） | 16 | LIBREF_SIMVEC_HNSW_M | 本地图索引连接数 |
| simvec_hnsw_ef_construct | 整数（≥4） | 100 | LIBREF_SIMVEC_HNSW_EF_CONSTRUCT | 本地图索引构造束宽 |
| simvec_full_scan_threshold | 整数（≥1） | 10000 | LIBREF_SIMVEC_FULL_SCAN_THRESHOLD | 本地精确扫描切到图索引的活跃点数阈值 |
| simvec_hnsw_ef_search | 整数（≥1） | 40 | LIBREF_SIMVEC_HNSW_EF_SEARCH | 本地图搜索束宽 |
| simvec_max_indexing_threads | 整数（≥1）/空 | 空 | LIBREF_SIMVEC_MAX_INDEXING_THREADS | 本地索引构建线程上限，空时由引擎决定 |
| qdrant_grpc | 布尔 | 关 | LIBREF_QDRANT_GRPC | 远端传输切到 gRPC，缺省使用 REST |
| qdrant_grpc_port | 整数（1–65535） | 6334 | LIBREF_QDRANT_GRPC_PORT | gRPC 端口 |
| qdrant_prefix | 字符串/空 | 空 | LIBREF_QDRANT_PREFIX | 远端集合名前缀 |
| qdrant_pool_size | 整数（≥1）/空 | 空 | LIBREF_QDRANT_POOL_SIZE | 远端连接池大小，空时沿用客户端默认 |
| qdrant_on_disk_vectors | 布尔/空 | 空 | LIBREF_QDRANT_ON_DISK_VECTORS | 集合创建时的向量落盘策略，变更会重建集合 |
| qdrant_on_disk_payload | 布尔/空 | 空 | LIBREF_QDRANT_ON_DISK_PAYLOAD | 集合创建时的载荷落盘策略，变更会重建集合 |
| qdrant_shard_number | 整数（≥1）/空 | 空 | LIBREF_QDRANT_SHARD_NUMBER | 集合分片数，变更会重建集合 |
| qdrant_replication_factor | 整数（≥1）/空 | 空 | LIBREF_QDRANT_REPLICATION_FACTOR | 集合副本数，变更会重建集合 |
| qdrant_write_consistency_factor | 整数（≥1）/空 | 空 | LIBREF_QDRANT_WRITE_CONSISTENCY_FACTOR | 写入一致性因子，不得超过副本数 |
| qdrant_hnsw_m | 整数（≥2）/空 | 空 | LIBREF_QDRANT_HNSW_M | 远端图索引连接数，变更会重建集合 |
| qdrant_hnsw_ef_construct | 整数（≥4）/空 | 空 | LIBREF_QDRANT_HNSW_EF_CONSTRUCT | 远端构造束宽，变更会重建集合 |
| qdrant_full_scan_threshold | 整数（≥1）/空 | 空 | LIBREF_QDRANT_FULL_SCAN_THRESHOLD | 远端精确扫描阈值，就地重写 |
| qdrant_memmap_threshold_kb | 整数（≥1）/空 | 空 | LIBREF_QDRANT_MEMMAP_THRESHOLD_KB | 远端段进入 mmap 的阈值，就地重写 |
| qdrant_indexing_threshold_kb | 整数（≥1）/空 | 空 | LIBREF_QDRANT_INDEXING_THRESHOLD_KB | 远端段开始建索引的阈值，就地重写 |
| metadata_backend | 字面量 | sqlite | LIBREF_METADATA_BACKEND | 元数据与关系会话后端：`sqlite/postgres` |
| fulltext_backend | 字面量 | fts5 | LIBREF_FULLTEXT_BACKEND | 全文后端：`fts5/postgres` |
| postgres_url | 密钥/空 | 空 | LIBREF_POSTGRES_URL | PostgreSQL 连接串，选用 postgres 后端时必填 |
| postgres_pool_size | 整数（≥1） | 8 | LIBREF_POSTGRES_POOL_SIZE | PostgreSQL 连接池大小 |
| postgres_timeout | 浮点（>0） | 10.0 | LIBREF_POSTGRES_TIMEOUT | PostgreSQL 语句超时，秒 |

向量是 SQLite 真源的可重建派生索引，无密钥时向量路禁用，纯全文检索。集合形态字段（距离、图参数、落盘、分片与副本）变化触发重建，搜索与连接参数就地生效。

## 分块（[chunking]）

嵌入轨道按字符类别折算令牌（字母 0.25、符号 1、空白 0.2、中日韩文字 1、其他文字 1，向上取整，
标定口径见 `docs/archive/token-estimation.md`），因此同一上限在中文、英文与代码上的实际令牌量相当；
全文轨道按词项计数，与全文索引切分一致。

| 字段 | 类型 | 默认 | 环境变量 | 影响 |
|---|---|---|---|---|
| chunk_embed_max_tokens | 整数（≥1） | 512 | LIBREF_CHUNK_EMBED_MAX_TOKENS | 嵌入轨道分块上限 |
| chunk_embed_overlap_tokens | 整数（≥0） | 50 | LIBREF_CHUNK_EMBED_OVERLAP_TOKENS | 嵌入轨道重叠 |
| chunk_embed_min_tokens | 整数（≥1） | 150 | LIBREF_CHUNK_EMBED_MIN_TOKENS | 嵌入轨道下限，不得超过上限 |
| chunk_bm25_max_terms | 整数（≥1） | 150 | LIBREF_CHUNK_BM25_MAX_TERMS | 全文轨道分块上限 |
| chunk_bm25_overlap_terms | 整数（≥0） | 5 | LIBREF_CHUNK_BM25_OVERLAP_TERMS | 全文轨道重叠 |
| chunk_bm25_min_terms | 整数（≥1） | 45 | LIBREF_CHUNK_BM25_MIN_TERMS | 全文轨道下限，不得超过上限 |
| chunk_reserve_ratio | 浮点（≥0） | 0.06 | LIBREF_CHUNK_RESERVE_RATIO | 分块预留比 |

抓取入库与仓库索引共用上述预算。

## 嵌入（[embeddings]）

| 字段 | 类型 | 默认 | 环境变量 | 影响 |
|---|---|---|---|---|
| embedding_model | 字符串 | BAAI/bge-m3 | LIBREF_EMBEDDING_MODEL | 嵌入模型名，记入版本元数据 |
| embedding_dim | 整数 | 1024 | LIBREF_EMBEDDING_DIM | 向量维度，变化触发集合重建 |
| embedding_api_base | 字符串 | https://api.siliconflow.cn/v1 | LIBREF_EMBEDDING_API_BASE | 嵌入接口地址 |
| embedding_api_key | 密钥/空 | 空 | LIBREF_EMBEDDING_API_KEY | 缺省禁用向量路 |
| embed_batch | 整数（≥1） | 64 | LIBREF_EMBED_BATCH | 嵌入批大小 |

模型名变化按版本懒重嵌，维度变化重建集合。

## 精排（[rerank]）

| 字段 | 类型 | 默认 | 环境变量 | 影响 |
|---|---|---|---|---|
| rerank_model | 字符串 | BAAI/bge-reranker-v2-m3 | LIBREF_RERANK_MODEL | 重排模型 |
| rerank_enabled | 布尔 | 关 | LIBREF_RERANK_ENABLED | 精排总开关 |
| rerank_candidate_limit | 整数（≥1） | 30 | LIBREF_RERANK_CANDIDATE_LIMIT | 候选下限 |
| rerank_top_n | 整数（≥1） | 10 | LIBREF_RERANK_TOP_N | 重排窗口上限，不得超过候选下限 |
| rerank_truncate | 整数（≥1） | 500 | LIBREF_RERANK_TRUNCATE | 重排输入截断，与材料上限取小 |

关闭时混合检索保留融合顺序并标记未启用。

## 检索（[retrieval]）

| 字段 | 类型 | 默认 | 环境变量 | 影响 |
|---|---|---|---|---|
| rrf_k | 整数（≥1） | 20 | LIBREF_RRF_K | 融合深度常数 |
| rrf_w_fts | 浮点（≥0） | 1.0 | LIBREF_RRF_W_FTS | 全文路权重 |
| rrf_w_vec | 浮点（≥0） | 1.0 | LIBREF_RRF_W_VEC | 向量路权重 |
| freq_alpha | 浮点（≥0） | 0.15 | LIBREF_FREQ_ALPHA | 序列支持度加权 |
| cluster_alpha | 浮点（≥0） | 0.05 | LIBREF_CLUSTER_ALPHA | 聚类加权 |
| doc_feature_alpha | 浮点（≥0） | 0.0 | LIBREF_DOC_FEATURE_ALPHA | 文档特征加权，默认关闭 |
| doc_feature_enabled | 布尔 | 开 | LIBREF_DOC_FEATURE_ENABLED | 抓取后重建文档派生特征 |
| doc_feature_candidate_budget | 整数（≥1） | 200 | LIBREF_DOC_FEATURE_CANDIDATE_BUDGET | 每块特征候选上限 |
| doc_feature_terms_per_facet | 整数（≥1） | 20 | LIBREF_DOC_FEATURE_TERMS_PER_FACET | 每个特征取值上限 |
| doc_feature_min_confidence | 浮点（0～1） | 0.4 | LIBREF_DOC_FEATURE_MIN_CONFIDENCE | 对齐边最低置信度 |
| doc_feature_enabled_facets | 字符串列表 | 全部 | LIBREF_DOC_FEATURE_ENABLED_FACETS | 允许落库的块特征面 |
| doc_consistency_min_support | 整数（≥1） | 2 | LIBREF_DOC_CONSISTENCY_MIN_SUPPORT | 漂移判定的最小模式支持度 |
| doc_consistency_llm_enabled | 布尔 | 关 | LIBREF_DOC_CONSISTENCY_LLM_ENABLED | 漂移说明的模型归纳开关 |
| doc_consistency_batch | 整数（≥1） | 8 | LIBREF_DOC_CONSISTENCY_BATCH | 漂移说明归纳批大小 |
| candidate_multiplier | 整数（≥1） | 3 | LIBREF_CANDIDATE_MULTIPLIER | 召回预算倍数 |
| assemble_multiplier | 整数（≥1） | 2 | LIBREF_ASSEMBLE_MULTIPLIER | 组装窗口倍数 |
| assemble_min | 整数（≥1） | 10 | LIBREF_ASSEMBLE_MIN | 组装窗口下限 |
| retrieval_max_versions | 整数（≥1） | 8 | LIBREF_RETRIEVAL_MAX_VERSIONS | 跨版本扇出上限，超限标记版本截断 |
| snippet_radius | 整数（≥0） | 60 | LIBREF_SNIPPET_RADIUS | 片段半径，须小于片段上限 |
| snippet_cap | 整数（≥1） | 400 | LIBREF_SNIPPET_CAP | 片段上限 |
| window_preview_cap | 整数（≥1） | 300 | LIBREF_WINDOW_PREVIEW_CAP | 窗口预览上限 |
| fts_w_content | 浮点（≥0） | 1.0 | LIBREF_FTS_W_CONTENT | 全文内容列权重 |
| fts_w_title | 浮点（≥0） | 5.0 | LIBREF_FTS_W_TITLE | 全文标题列权重 |
| fts_w_url | 浮点（≥0） | 1.0 | LIBREF_FTS_W_URL | 全文地址列权重 |
| fts_w_path | 浮点（≥0） | 3.0 | LIBREF_FTS_W_PATH | 全文路径列权重 |
| answer_strong_threshold | 浮点（0～1） | 0.8 | LIBREF_ANSWER_STRONG_THRESHOLD | 证据充分下限，达标记为可答，须不小于弱档 |
| answer_weak_threshold | 浮点（0～1） | 0.5 | LIBREF_ANSWER_WEAK_THRESHOLD | 证据弱档下限，低于此值记为不可答并弃答 |
| cluster_threshold | 浮点（≥0） | 0.15 | LIBREF_CLUSTER_THRESHOLD | 聚类阈值，挖掘与加权共用 |
| cluster_min_size | 整数（≥1） | 2 | LIBREF_CLUSTER_MIN_SIZE | 聚类最小规模 |
| facet_topn | 整数（≥1） | 50 | LIBREF_FACET_TOPN | 形态每层截断上限 |
| facet_candidate_budget | 整数（≥0） | 500 | LIBREF_FACET_CANDIDATE_BUDGET | 形态候选组合预算上限 |
| facet_min_gain | 浮点（≥0） | 0.0 | LIBREF_FACET_MIN_GAIN | 形态贡献占比过滤阈值 |
| guard_enabled | 布尔 | 关 | LIBREF_GUARD_ENABLED | 守卫特征开关，默认关闭 |
| facet_alpha | 浮点（≥0） | 0.05 | LIBREF_FACET_ALPHA | 形态检索加成权重 |
| drift_topn | 整数（≥1） | 20 | LIBREF_DRIFT_TOPN | 漂移清单截断数 |
| evidence_per_file_limit | 整数（≥1） | 1 | LIBREF_EVIDENCE_PER_FILE_LIMIT | 每文件证据上限（挖掘读取按此值解析） |
| evidence_dedup | 布尔 | 开 | LIBREF_EVIDENCE_DEDUP | 证据去重默认 |
| induction_enabled | 布尔 | 关 | LIBREF_INDUCTION_ENABLED | 标签归纳开关 |
| induction_topn | 整数（≥1） | 20 | LIBREF_INDUCTION_TOPN | 归纳数量 |
| induction_batch | 整数（≥1） | 8 | LIBREF_INDUCTION_BATCH | 归纳批大小 |
| induction_prompt_version | 字符串 | v1 | LIBREF_INDUCTION_PROMPT_VERSION | 归纳提示版本 |
| retriever_window_radius | 整数（≥0） | 1 | LIBREF_RETRIEVER_WINDOW_RADIUS | 窗口半径默认 |
| retriever_expand_window | 布尔 | 关 | LIBREF_RETRIEVER_EXPAND_WINDOW | 窗口默认开关，详尽预设强制开 |

三预设共用以上数值：精确跳过向量、重排、挖掘加权、窗口与标签；均衡全走但窗口关闭；详尽在均衡上打开窗口。详尽只补邻块、不扩大召回，更广召回调大条数。

## 注册表（[registry]）

| 字段 | 类型 | 默认 | 环境变量 | 影响 |
|---|---|---|---|---|
| registry_cache_ttl_days | 浮点（≥0） | 1.0 | LIBREF_REGISTRY_CACHE_TTL_DAYS | 解析缓存有效期，天 |
| registry_offline | 布尔 | 关 | LIBREF_REGISTRY_OFFLINE | 离线模式，无缓存直接报错 |
| registry_allow_private | 布尔 | 关 | LIBREF_REGISTRY_ALLOW_PRIVATE | 允许私有包解析 |
| registry_mirror | 字符串 | default | LIBREF_REGISTRY_MIRROR | 镜像预设 |
| registry_bases | 字典 | 六生态默认地址 | LIBREF_REGISTRY_BASES | 各生态基地址，优先于镜像 |
| registry_overrides_file | 路径/空 | 空 | LIBREF_REGISTRY_OVERRIDES_FILE | 外部地址覆盖文件 |
| registry_override_ttl_days | 浮点（≥0） | 180.0 | LIBREF_REGISTRY_OVERRIDE_TTL_DAYS | 覆盖有效期，天 |

相关命令：`libref local index-repo` 与 `librefctl resolve/discover` 及 MCP/Web 的同名工具。

## 发现（[discovery]）

| 字段 | 类型 | 默认 | 环境变量 | 影响 |
|---|---|---|---|---|
| discovery_enabled | 布尔 | 开 | LIBREF_DISCOVERY_ENABLED | 模糊发现总开关 |
| discovery_topn | 整数（1–100） | 10 | LIBREF_DISCOVERY_TOPN | 发现条数上限 |
| discovery_cache_ttl_days | 浮点（≥0） | 1.0 | LIBREF_DISCOVERY_CACHE_TTL_DAYS | 发现缓存有效期，天 |
| discovery_offline | 布尔 | 关 | LIBREF_DISCOVERY_OFFLINE | 离线模式直接拒绝 |
| discovery_github_token | 密钥/空 | 空 | LIBREF_DISCOVERY_GITHUB_TOKEN | 代码托管搜索令牌 |

## 问答（[chat]）

| 字段 | 类型 | 默认 | 环境变量 | 影响 |
|---|---|---|---|---|
| chat_model | 字符串 | Qwen/Qwen3.5-4B | LIBREF_CHAT_MODEL | 问答模型 |
| chat_enabled | 布尔 | 关 | LIBREF_CHAT_ENABLED | 问答总开关，关闭时问答返回不可用 |
| chat_api_base | 字符串 | https://api.siliconflow.cn/v1 | LIBREF_CHAT_API_BASE | 问答接口地址 |
| chat_api_key | 密钥/空 | 空 | LIBREF_CHAT_API_KEY | 缺省问答不可用 |
| chat_timeout | 浮点（>0） | 60.0 | LIBREF_CHAT_TIMEOUT | 问答超时，秒 |
| chat_enable_thinking | 布尔 | 关 | LIBREF_CHAT_ENABLE_THINKING | 思考模式开关 |
| material_limit | 整数（≥1） | 1500 | LIBREF_MATERIAL_LIMIT | 单材料上限 |
| answer_total_budget | 整数（≥1） | 6000 | LIBREF_ANSWER_TOTAL_BUDGET | 回答材料总预算（硬上限），按排序分配，超出部分整条丢弃并以省略标记告知模型 |

## 服务暴露（[mcp]、[auth]、[web]）

| 字段 | 类型 | 默认 | 环境变量 | 影响 |
|---|---|---|---|---|
| mcp_path | 字符串 | /mcp | LIBREF_MCP_PATH | MCP 在组合 Web 监听上的挂载路径 |
| mcp_enabled_tools | 名单/空 | 空（全量） | LIBREF_MCP_ENABLED_TOOLS | 工具白名单，空数组表示零工具 |
| mcp_disabled_tools | 名单 | 空 | LIBREF_MCP_DISABLED_TOOLS | 工具黑名单，优先于白名单 |
| auth_mode | 字面量 | enforced | LIBREF_AUTH_MODE | `dev/enforced`，默认强制鉴权；dev 必须显式开启且只许回环 |
| dev_principal | 字符串 | dev-operator | LIBREF_DEV_PRINCIPAL | dev 模式注入的主体名 |
| allowed_hosts | 名单/空 | 空 | LIBREF_ALLOWED_HOSTS | MCP 传输安全的主机表 |
| allowed_origins | 名单/空 | 空 | LIBREF_ALLOWED_ORIGINS | 浏览器来源精确允许表，禁止通配符与路径 |
| bootstrap_admin_key | 密钥/空 | 空 | LIBREF_BOOTSTRAP_ADMIN_KEY | 首次引导平台管理员，明文不落文件 |
| api_key_default_ttl_hours | 正数 | 168 | LIBREF_API_KEY_DEFAULT_TTL_HOURS | 未指定过期时间的 Key 默认有效期 |
| api_key_max_ttl_hours | 正数 | 8760 | LIBREF_API_KEY_MAX_TTL_HOURS | Key 最大有效期，默认有效期不得超过此值 |
| instance_id | 字符串 | libref-1 | LIBREF_INSTANCE_ID | DB 认领与版本锁的进程标识 |
| web_host | 字符串 | 127.0.0.1 | LIBREF_WEB_HOST | Web 监听地址 |
| web_port | 整数（1–65535） | 8001 | LIBREF_WEB_PORT | Web 监听端口 |
| web_spa_enabled | 布尔 | true | LIBREF_WEB_SPA_ENABLED | 是否启用前端外壳与静态资产托管 |
| web_spa_dir | 路径/空 | 空 | LIBREF_WEB_SPA_DIR | 前端产物目录；镜像默认指向内置 `dist`，缺 `index.html` 时退回纯 API |

未知工具名启动即报错。所有 HTTP 出口共用一套鉴权：enforced 下每请求需 Bearer Key，带 Origin 的浏览器请求还必须命中 `allowed_origins`；健康检查与静态资源除外。开发模式只供本机显式使用，反向代理到回环地址不改变模式。

## 治理（[retention]）

| 字段 | 类型 | 默认 | 环境变量 | 影响 |
|---|---|---|---|---|
| snapshot_enabled | 布尔 | 关 | LIBREF_SNAPSHOT_ENABLED | 抓取快照开关 |
| snapshot_dir | 路径 | ~/.local/share/libref/snapshots | LIBREF_SNAPSHOT_DIR | 快照目录 |
| snapshot_quota_mb | 整数（≥0） | 512 | LIBREF_SNAPSHOT_QUOTA_MB | 快照配额，兆 |
| repos_dir | 路径 | ~/.local/share/libref/repos | LIBREF_REPOS_DIR | 仓库目录 |
| repos_quota_mb | 整数（≥0） | 2048 | LIBREF_REPOS_QUOTA_MB | 仓库配额，兆 |
| deletion_log_ttl_days | 浮点（≥0） | 30.0 | LIBREF_DELETION_LOG_TTL_DAYS | 删除记录保留，天 |

## 配额（[quota]）

| 字段 | 类型 | 默认 | 环境变量 | 影响 |
|---|---|---|---|---|
| quota_enabled | 布尔 | 关 | LIBREF_QUOTA_ENABLED | 是否启用配额判定 |
| quota_http_requests_per_hour | 整数（≥0） | 0 | LIBREF_QUOTA_HTTP_REQUESTS_PER_HOUR | 每小时 HTTP 请求数上限，0 不限 |
| quota_llm_tokens_per_day | 整数（≥0） | 0 | LIBREF_QUOTA_LLM_TOKENS_PER_DAY | 每日 LLM 总 token 上限，0 不限 |
| quota_job_submissions_per_hour | 整数（≥0） | 0 | LIBREF_QUOTA_JOB_SUBMISSIONS_PER_HOUR | 每小时任务提交上限，0 不限 |
| quota_index_requests_per_hour | 整数（≥0） | 0 | LIBREF_QUOTA_INDEX_REQUESTS_PER_HOUR | 每小时索引申请上限，0 不限 |

数据库策略优先；配置字段只是无匹配策略时的兜底上限。策略行经 `admin:config` 的配额接口（`GET/PUT /admin/quota/policies`）读写，按租户与主体覆盖，写入后即时生效。

## 日志（[logging]）

| 字段 | 类型 | 默认 | 环境变量 | 影响 |
|---|---|---|---|---|
| log_level | 字面量 | WARNING | LIBREF_LOG_LEVEL | `DEBUG/INFO/WARNING/ERROR` |
| log_file | 路径/空 | 空 | LIBREF_LOG_FILE | 日志文件，缺省只写标准错误 |
| log_format | 字面量 | text | LIBREF_LOG_FORMAT | `text/json` |
