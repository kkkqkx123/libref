# code-context-engine 分段（chunking）设计分析

> 仓库：本地 `~/code/code-context-engine`（Rust，模块化 workspace）。
> 范围：**只分析代码分段与嵌入索引的设计**，其"AST→自然语言转换"（ast_to_nl 的 LLM/规则转写）部分不在参考范围。
> 入口样本：`crates/app/cce-e2e-tests` 的 direct-chunking 实现（`src/direct_chunker.rs`、`examples/rust/dbg_direct_chunks.rs`）。
> 与本项目的对应：libref 的 `chunking/`（文档分块）、`astminer/`（源码实体抽取）、`store/`（FTS5 BM25 + sqlite-vec 双路检索，module-layout.md §1）。

## 1. 总体流水线

```
ParseCoordinator.parse (tree-sitter)
  → PreprocessingPipeline.process  → Vec<EntityGroup>   // 实体分组（类+方法、文件头+顶层项）
  → build_group_conversions        → Vec<GroupConversions>  // 每个 group → header + members，携带原始源码 span
  → GroupChunker.chunk_groups      → Vec<ChunkedResult>     // 双路分块：Bm25 / Embedding
```

direct-chunking（`cce-e2e-tests/src/direct_chunker.rs::EntityChunker`）的关键点：**跳过 NL 转换，直接把实体的原始源码文本作为 chunk text** 喂给同一个 `GroupChunker`（`raw_source_conversion` 中 `bm25_text` 与 `embedding_text` 均填 `source`）。这证明其 chunker 是与文本来源解耦的：不管上游给的是转写文本还是原始代码，预算/切分/合并逻辑完全复用。

## 2. 最值得参考的设计

### 2.1 一套 chunker、两条检索路径（ChunkPath 判别式）

`cce-types/src/types/ast_to_nl/chunk_path.rs`：`ChunkPath::{Bm25, Embedding}` 不是文件路径，而是**检索管线判别式**——同一个分组同时产出两股 chunk 流：

| 参数 | Embedding 路 | BM25 路 |
|---|---|---|
| 硬上限 | 512 tokens | 150 terms |
| 重叠 | 50 tokens | 5 terms |
| 最小 chunk | 150 tokens | 45 terms |
| 计量单位 | token（嵌入模型口径） | term（与检索索引**同一个分词器**计数，CJK 按词计） |

- 配置见 `cce-config/src/modules/ast_to_nl.rs::ChunkingConfig`。
- 两条路的 chunk 用 `EmbChunk` / `Bm25Chunk` 新类型包装（`chunker.rs`），**编译期保证** BM25 chunk 不进 cross-group merge / overlap 流程。
- `chunk_id = {source_group_id}_{path}_{index}`，且同一 group 在两路共享 `source_group_id`，可互相锚定。

**对 libref 的意义**：`store/retriever.py` 本就规划 FTS5 BM25 + vec KNN 双路。不要"一份 chunk 同时进两个索引"，而是**同一次切分产出两种预算口径的 chunk**——BM25 路按 term 数上限走小块，嵌入路按 token 上限走，`documents_fts` 与 `documents_vec` 各存各的行，靠共享的源 span（或 section id）对齐。libref 文档分块是中英混排，"BM25 计量用与 FTS5 相同的 tokenizer"这条尤其要抄：否则 150 terms 的预算按空白切词计会严重失真。

### 2.2 预算模型：hard_limit 与 planning reserve 分离

`ChunkBudget`（config 模块）明确区分：
- **hard limit**：唯一对最终 chunk 生效的检查，绝不越过；
- **reserve**（如 `glossary_reserve_ratio=0.06`）：切分器的"规划目标"，为后追加的元数据留位，**不是超限许可**；
- 追加前仍按绝对硬限校验。

以及 `limits.rs` 的两级检查：预注入检查只记录违规并保留 chunk（advisory），硬限检查截尾——**截断必须告警、不静默、保留文件而不是丢弃**。

**对 libref 的意义**：`chunking/splitter.py` 规划"标题路径结构切分 + 贪心合并 + 代码围栏平衡"。建议同样把"为标题路径前缀/frontmatter 预留的 token"建模为 reserve，"chunk 最终不得超过 embedding 模型窗口"建模为 hard limit；截尾必须打日志计数（对应 AGENTS.md"不得静默放宽"的测试门禁精神）。

### 2.3 切分→段限→合并的职责分层

- `splitter.rs`：递归 `split_range`，**保证**产出的每片都不超路预算；切分尊重实体边界（`respect_boundaries`）。
- `segment_limit.rs`：对"手工构造的超长段"复用同一个 `split_range` 递归，而非另写一套按计数兜底的分支——单一递归入口，避免两套切分逻辑漂移。
- `merge.rs`：低于 min 阈值的 chunk 与**相邻**（按源码位置序、仅相邻 group）chunk 合并；合并 chunk 保留左 chunk 身份，所有贡献组记入 `merged_group_ids`——**溯源不丢**。
- `single_path.rs`：单路流程编排（limit 检查→切分→段限→组装）独立成模块，"哪些组参与分块、如何合并"的策略留在 chunker 层。

**对 libref 的意义**：贪心合并阶段直接对标 merge.rs 的三条约束——只并相邻、保留左身份、记录被并者集合（libref 的 documents 表可加 `merged_from` 列或 JSON 字段），否则审核工作流（services/review）会丢掉被合并 section 的归属。

### 2.4 单向重叠（Previous-only）

`OverlapType` 注释明确：当前只做**前向**重叠（每 chunk 存其前驱的尾部），`Next` 仅保留兼容——避免相邻 chunk 内容双向重复。`OverlapRegion` 记录 text、token 数、来源 chunk id 与字节区间。

**对 libref 的意义**：文档分块常犯的错是 prev/next 都放重叠导致检索去重困难。抄"只向前重叠 + 重叠区带来源 id"即可。

### 2.5 溯源精度显式化：SourceSpanKind

`cce-types/.../chunked.rs::SourceSpanKind`：chunk 的字节区间有五种**诚实级别**——
`exact_entities`（完整实体）/ `enclosing_entity`（硬限截断片段，只能声明所属实体）/ `group_fallback`（无法恢复归属，退到组 span）/ `document_range`（文档区间）/ `unavailable`。

配套的 `self_contained` 标志：自带 docstring 描述的成员 chunk 豁免跨组合并，避免主题被稀释。

**对 libref 的意义**：libref 的溯源粒度问题与 graphify.md §3 同源。建议 documents/api_entities 的行号区间列配一个类似的精度枚举（至少区分"完整实体截取"与"窗口截断"），审核界面据此决定是否展示"行号可能不精确"。

### 2.6 direct-chunking 本身：无 LLM 基线的工程价值

`EntityChunker` = parser + grouper + GroupChunker 的最小组装（约 160 行），即"**纯结构化、零 LLM** 的代码分块基线"。cce-e2e-tests 用它产出 BM25 chunk dump（`dbg_direct_chunks.rs`）做人工 review，并作为 `baselines/entity_based.rs` 等检索基线的共同底座。

**对 libref 的意义**：
1. libref 全程无 LLM，direct-chunking 正是同构形态：`astminer/` 抽出实体（graphify 模式）后，**直接以实体源码为 chunk** 入 `documents_vec/documents_fts`（或独立 api_* 表），无需任何转写。
2. cce 的做法提示：为分块质量建立**可视化 dump 工具**（libref 可做一个 `uv run python -m libref.debug.dump_chunks` 类 CLI，输出 file→chunks 的行号/字节/title 预览），配合 `tests/test_retriever.py` 的 MRR/Recall@k 门禁使用。

### 2.7 边界防御：空 span 在源头拦截

`direct_chunker.rs::build_group_conversions`：空白 span（纯注释区）在源头 `trim().is_empty()` 即跳过，注释写明原因——空文本流入 chunk 会被 embedding 服务以晦涩的 400 拒掉。分组 header 无成员源码时降级为"仅身份"转换（以组名为文本，组名有成员时必非空，注释里给了不变量论证）。

**对 libref 的意义**：system boundary 处校验的原则实例。libref 在 `services/ingest.py` 落库前应做同样的"空/纯空白 chunk 拦截 + 计数"，而不是等 embedding 层报错。

## 3. 不建议照搬的部分

- **Glossary 术语注入**（`glossary_*` 配置、`Vocabulary` 插件）：服务于 NL chunk 的领域词展开，libref 文档分块无此需求。
- **插件注册表**（`PluginRegistry` 的 Chunk 覆盖能力）：单项目自用无需插件化，libref 用 `astminer/config.py` 的声明式 LanguageConfig 即可。
- **组级 header_conversion / GroupRelation 跨组关系图**：依赖其 EntityGroup 模型与关系抽取；libref 文档分支用 section.path 天然有序，源码分支的调用关系已由 astminer 落 `call_instances`，不必在 chunk 层重复建图。
- **Rust 的 Mutex 包裹与 Arc 共享**：Python 单线程批处理不需要；libref 对应物是 `services/jobs.py` 的队列语义。

## 4. 落地建议摘要（映射到 module-layout.md）

| libref 目标模块 | 参考点 | 动作 |
|---|---|---|
| `chunking/splitter.py` | §2.2 预算模型、§2.3 合并三约束、§2.4 单向重叠 | 贪心合并只并相邻 section；被并者记入 merged_from；重叠只向前 |
| `store/`（FTS+vec） | §2.1 双路双预算 | 一次切分产出 BM25 小块与嵌入大块两股流，共享 section id 对齐；BM25 计量用 FTS5 同款 tokenizer |
| `astminer/` | §2.6 direct-chunking、§2.5 SourceSpanKind | 实体源码直接作 chunk（零转写）；span 精度枚举入库 |
| `services/ingest.py` | §2.7 空 span 拦截 | 落库前拦截空/纯空白 chunk 并计数告警 |
| `cli.py` / tests | §2.6 dump 工具 | 增加 chunk dump 调试出口，配合 MRR/Recall@k 门禁 |
