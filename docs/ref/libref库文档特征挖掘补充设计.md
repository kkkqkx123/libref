# libref 库文档特征挖掘补充设计（专项设计文档）

> 本文是《libref 特征挖掘功能评估与补充设计方案》的库文档侧专项展开，回答一个聚焦问题：**针对库文档（官方文档链路），是否需要补充特征挖掘？若需要，如何设计？** 文中"库文档"特指文档索引链路产出的知识：`pages` / `documents` / `documents_fts` / Qdrant 向量点（docs/architecture/doc-indexing-chain.md），与代码挖掘链路（Branch B：调用实例 → 序列模式/特征树/聚类）相对。
>
> 日期：2026-11-22 ｜ 依据仓库提交：bdc0373 ｜ 格式：Markdown（后续交付默认格式）

---

## 一、问题界定：库文档侧的现状

libref 是 library-reference 知识库系统，双链路并行：

| 链路 | 真源 | 派生产物 | 特征挖掘现状 |
|---|---|---|---|
| 文档链（库文档） | pages / documents / FTS | Qdrant 向量点（可重建） | **无自有特征派生产物** |
| 代码链（社区用法） | call_instances / api_entities | seq_patterns / feature_nodes / api_clusters / pattern_labels | 四件套齐备（feature_tree.py、cluster.py 等） |

库文档侧当前只有"采集 → 净化 → 分块 → 落库 → 检索/问答"的管道能力，三个具体事实构成问题的出发点：

1. **文档块实体名单为空。** 分块规范明确"块携带实体名单字段，文档块为空，源码块由抽取侧回填"（doc-indexing-chain.md §3）。即文档块与 API 实体之间没有任何结构化关联，全靠检索时的词面/向量相似。
2. **检索已消费代码侧挖掘产物，但方向是单向的。** 混合检索含"挖掘加权"环节（`freq_alpha` / `cluster_alpha`，以乘法叠加在聚合分上，docs/bench/design.md §25），用社区用法的频次与聚类给文档页面加权——文档侧自己不产出任何特征。
3. **文档自身的结构与语义特征未被挖掘。** 示例代码块、参数表、版本差异、废弃（deprecated）标记、跨页主题结构这些对检索与问答高价值的形态，目前在分块后只作为纯文本/向量存在。

## 二、必要性分析：是否需要补充？

**结论：需要补充，但范围应严格限定为"文档侧特征 + 文档-代码对齐"两条线；不需要在文档侧重做序列模式挖掘。**

### 2.1 需要补的三条理由

| # | 缺口 | 影响 | 受益方 |
|---|---|---|---|
| 1 | 文档块实体回填缺失，doc ↔ api_entities 对齐断裂 | 挖掘加权只能作用在"页面"粒度；问答引用无法标注"这段文档讲的是哪个 API"；跨链路溯源断在块级 | 检索加权、问答引用、MCP 出口 |
| 2 | 文档自身结构特征缺失（块类型、示例代码、参数表、版本/废弃标记） | "有示例的页面优先""避开废弃 API"这类高价值排序与过滤信号不存在；详尽预设的窗口扩展也无法按块类型智能取窗 | 检索排序、过滤、问答材料组装 |
| 3 | 文档-用法一致性（doc drift）无度量 | 官方文档所述用法与社区挖掘出的真实用法之间有何差距、哪些 API 文档滞后于实践——不可量化，而这恰是 libref 作为"库参考"系统的核心增值点 | 质量治理、版本对比、产品差异化 |

三条都指向同一个判断：**库文档的价值不在"再挖一遍调用序列"，而在把官方文档从"可检索的文本"升格为"带特征、可对齐、可度量的知识层"**，与代码侧挖掘产物形成互补证据（官方说法 vs 社区实践）。

### 2.2 明确不做的三件事

1. **不在文档块上做调用序列模式挖掘（PrefixSpan 一类）。** 文档叙述不携带调用序列语义，示例代码片段也天然稀疏；序列模式的真源应继续锚定 `call_instances`，文档侧只做"模式覆盖对齐"（见 D3）。
2. **不推翻既有检索管线。** 特征以加权项/过滤项形式接入混合检索（与 freq_alpha/cluster_alpha 同范式），不改双路召回与 RRF 融合的骨架。
3. **不引入重型外部依赖。** 沿用项目硬约束：纯内存算法、档案（mining_profiles/versions）隔离、SQLite 真源、LLM 只做归纳层（命名/摘要），不做端到端生成。

## 三、功能设计

### 3.1 特征体系：D1–D4 四层

| 层 | 名称 | 特征内容 | 构建方式 | LLM 参与 |
|---|---|---|---|---|
| D1 | 结构特征 | 块类型（叙述/示例/参数表/警告/变更日志）、标题深度、块长、围栏代码占比、是否含表格 | 规则 + 启发式（围栏/表格/标题路径检测），纯内存 | 否 |
| D2 | 内容特征 | 关键词、术语候选、示例代码的语言/符号清单、参数名/返回值候选 | 规则抽取 + 既有实体解析器回填；示例代码走轻量 AST（对齐 spec/astminer-captures.md 口径） | 否（命名归一可选 LLM 归纳） |
| D3 | 对齐特征 | doc↔api_entities 关联、doc↔seq_patterns 覆盖度（该页示例覆盖了哪些已挖模式）、doc↔feature_nodes 挂接 | 以 D2 符号清单对 api_entities 做确定性匹配；模式覆盖以序列包含判定 | 否 |
| D4 | 一致性/漂移特征 | 文档所述用法与社区模式的匹配/背离、废弃标记命中、跨版本特征漂移对比 | D3 结果的差集/比对统计；LLM 只对"背离样本"做归纳命名 | 是（归纳层） |

设计要点：
- **D1/D2 全部确定性可重放**（缓存键沿用"种类 + 预算指纹 + 原文哈希"口径），D4 的 LLM 产物按 pattern_labels 同款方式落库、可整版清除。
- D3 是本次补充的**最高优先级**：它直接修复 §一.1 的实体回填缺口，且成本最低（符号匹配是纯内存操作）。
- 借鉴 OpenFE 的 expand-and-reduce 思路：先穷举候选特征（expand），再按检索增益做显著性过滤（reduce），避免特征表无界膨胀（见 §四 OpenFE、tsfresh 条目）。

### 3.2 数据模型（新增派生表，风格对齐 docs/features/db/derived.md）

| 表 | 主键/列草案 | 说明 |
|---|---|---|
| doc_features | (version_id, document_id, facet) + value JSON | D1/D2 特征面，按版本隔离，整版清除顺序接在 pages 删除前 |
| doc_api_links | (version_id, document_id, api_entity_id) + match_kind + confidence | D3 对齐边；match_kind ∈ {symbol, signature, name_fuzzy}；同版本唯一 |
| doc_pattern_coverage | (version_id, document_id, pattern_id) + covered_kind | D3 模式覆盖；covered_kind ∈ {example_code, prose} |
| doc_consistency | (version_id, document_id, facet) + verdict + note | D4 一致性结论；verdict ∈ {match, drift, unknown}；LLM 归纳写 note |

约束与约定（沿用项目既有纪律）：
- 真源一律 SQLite，四表均为可重建派生表；向量库只存可重建索引，正文回 SQLite 取。
- 按挖掘档案/版本隔离；审核冲突行不进入构建；整版清除按 version_id 过滤，顺序：doc_consistency → doc_pattern_coverage → doc_api_links → doc_features → pages 级联。
- 迁移登记为新版本号（当前至 12，新增 13: doc-feature-facets），单事务执行、重放幂等。

### 3.3 管线：文档侧派生阶段

在既有 ingest 链（发现 → 抓取 → 净化/分块 → 落库）之后追加**派生阶段**，独立可重跑：

```
分块完成的 documents 行
  → [P1] D1 结构标注（规则，逐块）
  → [P2] D2 内容抽取（示例代码围栏捕获 → AST 符号 → 实体解析回填）
  → [P3] D3 对齐构建（符号匹配 → doc_api_links；模式包含判定 → doc_pattern_coverage）
  → [P4] D4 一致性评分（差集统计 → 背离样本集 → LLM 归纳命名 → doc_consistency）
  → 向量同步不变（maintenance.py 共享函数），特征经读模型出口
```

- P1–P3 纯内存、可并行于抓取流水；P4 的 LLM 调用只对"背离样本"触发，失败只计数告警，不推翻真源（对齐 doc-indexing-chain.md §4 的失败纪律）。
- 示例代码抽取复用 `chunking/splitter.py` 已保护的围栏块，不二次切分正文。
- 任务化走既有 `jobs.py`（JobManager），恢复语义同抓取任务。

### 3.4 出口与检索接入

| 出口 | 变更 |
|---|---|
| 检索加权 | 新增 `doc_feature_alpha`（示例命中/实体对齐加成），与 freq_alpha/cluster_alpha 同为乘法叠加项，纳入 bench 判定集（docs/bench/design.md 已预留"挖掘加权判定集"口径，本次补文档侧档位） |
| 过滤/排序 | 新增过滤维度：块类型（只要示例）、废弃标记排除、仅含对齐实体的页面 |
| MCP | `pattern_insight` 旁挂 `doc_insight` 视图：doc_features / links / coverage / consistency 四个子视图，形态对齐 pattern_insight 七视图 |
| CLI/Web | 详尽预设打开 D3 加成；页面详情展示对齐实体与覆盖模式 |

### 3.5 评估

- **golden 集**：每库抽 30–50 个查询，标注"应命中的文档块 + 应关联的 API 实体"，度量 D3 回填准确率（目标 ≥ 0.9）与检索 nDCG 增益。
- **一致性抽查**：对 D4 判定为 drift 的样本人工抽检，误报率 < 20% 才放行到出口。
- **回归护栏**：对齐 docs/archive/retrieval-bench-data.md 的口径——"挖掘加权需要专门的判定集"，本次为文档侧补同样性质的判定集，避免再现"四档参数指标完全一致、不可标定"的老问题。

## 四、可参考的项目 / 论文清单

按三条借鉴线分组；标 ★ 为本设计直接采用其思路的条目。

### 4.1 文档-代码对齐与一致性（D3/D4 直接来源）

| 成果 | 简述 | 可借鉴点 |
|---|---|---|
| ★ DocChecker（Dau, Bui 与 Guo，EACL 2024 Demo；arXiv:2306.06347） | 以 UniXcoder 为骨干的代码-注释一致性检测与生成工具，ICCD 任务 72.3% 准确率，支持 10 种语言 | "一致性判定 + 修复建议"的成对输出形态；对比学习对齐代码与文本特征空间的思路可降级为符号匹配版用于 D4 |
| ★ ExampleCheck（Chen 等，2020） | 用 API 文档的使用约束检查示例与用户代码中的误用 | 文档约束（先决条件/参数约束）的抽取口径，D4 的"文档所述用法"侧依据 |
| APISan（Yun 等，USENIX Security 2015） | 从海量调用中挖掘隐式惯例（参数取值、调用顺序），检测偏离惯例的疑似 bug | "惯例 = 多数实践"的统计口径，可反向用于度量文档示例是否偏离社区惯例 |
| SoAy（2022） | 从问答社区挖掘 API 解决方案，与官方文档互补 | "官方文档 + 社区实践"双源互补的论证方式，支撑本文必要性判断 |
| KDRAR（2023） | 基于知识图谱的 API 推荐，融合文档与用法知识 | 文档实体挂接知识图谱的组织方式，D3 对齐边的图谱化表达 |
| DocChecker 前作谱系：Comment inconsistency 检测（He 等；Liu 等，ICSE 2018 一系） | 启发式规则检测代码-注释漂移 | "文档腐烂（documentation rot）"概念直接支撑 D4 漂移度量的命名与立意 |

### 4.2 文档结构与层级特征（D1/D2 来源）

| 成果 | 简述 | 可借鉴点 |
|---|---|---|
| ★ RAPTOR（Sarthi 等，ICLR 2024） | 递归聚类 + LLM 摘要构建多层摘要树，检索可跨抽象层级 | "树 = 可检索单元"与 libref 特征树同构；文档主题树可作为 D1 的高层形态（远期），其 GMM 软聚类 + BIC 定簇思路可借给文档主题聚类 |
| GraphRAG（Microsoft，2024） | 实体关系图 + 社区摘要回答多跳/全局问题 | 实体抽取-建图-社区摘要三段式，供 doc_api_links 图谱化后的聚合摘要参考 |
| BERTopic（Grootendorst，2022） | 基于嵌入与 c-TF-IDF 的主题建模，可产出主题词与层级 | 文档块主题标签的低成本实现路线（D2 术语候选） |
| Uddin 与 Robillard（MSR 2015） | API 文档信息类型与缺失问题的实证研究 | 文档特征分类学（参数/返回/异常/示例/版本）直接校准 D1 块类型清单 |
| CodeSearchNet（Husain 等，NeurIPS 2019） | 代码-自然语言对齐的大规模数据集与基准 | doc↔code 对齐任务的评估协议（MRR/Recall@k）可平移为 D3 评估口径 |

### 4.3 特征工程方法论（expand-and-reduce、质量门禁）

| 成果 | 简述 | 可借鉴点 |
|---|---|---|
| ★ OpenFE（Zhang 等，KDD 2023） | 自动特征工程：两阶段 expand（LLM/枚举生成候选）+ reduce（逐特征增益筛选） | D1–D4 候选特征的准入-淘汰机制；防止 doc_features 表无界膨胀 |
| tsfresh（Christ 等，2016） | 时序特征库 + 统计显著性过滤 | "先穷举后显著性过滤"的工程范式；特征价值打分可平移为检索增益打分 |
| Featuretools（Kanter 与 Veeramachaneni，KDD 2018） | 深度特征合成（DFS），实体关系上的自动化特征生成 | 实体-关系视角（libraries→versions→pages→documents）上的特征合成思路，指导 doc_api_links 上的聚合特征（如"该 API 被多少文档页覆盖"） |
| PrefixSpan / CloSpan / BIDE（Pei 等 2004 等） | 闭合序列模式挖掘算法族 | 不用于文档侧本身，但 D3"模式覆盖判定"复用其序列包含语义，保持与代码侧口径一致 |

### 4.4 与主报告引用清单的关系

主报告（libref 特征挖掘功能评估与补充设计方案）第三章已收录 API 用法挖掘谱系（MAPO、UP-Miner、PAM、GROUMINER、BigGroum、MUDetect 等）与代码特征表示（EpiCoder、RPG-Encoder、CodeScholar）。本清单聚焦**文档侧**增量，两边合并即为完整可借鉴成果集（约 28 项）。

## 五、实施路线

| 优先级 | 内容 | 粗估 | 交付判据 |
|---|---|---|---|
| P0 | D3 对齐：doc_api_links + 文档块实体回填；检索加权接 doc_feature_alpha 最小档 | 1–2 周 | golden 集对齐准确率 ≥ 0.9；文档块实体名单非空率显著提升 |
| P1 | D1/D2 结构与内容特征 + 块类型过滤出口；doc_features 表与迁移 13 | 2–3 周 | 块类型标注抽检一致率 ≥ 0.85；示例过滤在 golden 集上有 nDCG 增益 |
| P2 | D4 一致性/漂移：doc_pattern_coverage + doc_consistency，LLM 归纳层接入；跨版本漂移对比 | 2–4 周 | drift 样本误报率 < 20%；MCP doc_insight 四视图齐备 |
| P2+ | 文档主题树（RAPTOR 式）试验、GraphRAG 式实体图谱聚合 | 待评估 | 单独立项，不阻塞前三档 |

配套文档动作（建议与代码同步落）：
1. 新增 `docs/features/doc-feature-mining.md`（本文精简为功能篇）；
2. `docs/features/db/derived.md` 增补四张新派生表条目；
3. `docs/architecture/doc-indexing-chain.md` §5 检索语义补 doc_feature_alpha 说明；
4. `docs/bench/design.md` 补文档侧挖掘加权判定集要求。

## 六、风险与边界

- **特征膨胀**：以 OpenFE 式 reduce 硬闸控制 doc_features 行数，超预算特征整批丢弃并计数（对齐向量同步"超限截尾计数"纪律）。
- **LLLM 归纳失真**：D4 note 属可重建派生数据，纳入 pattern_labels 同款整版清除与审核流；不得进入真源。
- **检索收益不确定**：主报告评测已出现"挖掘加权四档参数指标完全一致、不可标定"的先例（docs/archive/retrieval-bench-data.md）；本次将判定集建设列为 P0 的伴随交付，若两周内无法标定收益，则只保留 D3 对齐的溯源价值、暂缓加权项上默认预设。
