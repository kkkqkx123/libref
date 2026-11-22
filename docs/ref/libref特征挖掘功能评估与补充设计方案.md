# libref 特征挖掘功能评估与补充设计方案

## 一、背景与评估范围

libref 是一个 library-reference 知识库系统：爬取库官方文档、挖掘开源社区对同一库的真实用法，经 CLI、MCP、Web 三出口向人与 AI 助手提供可溯源的 API 知识（来源：AGENTS.md；docs/req/init-req.md）。其源码挖掘分支（Branch B）在调用实例之上做频次、共现与序列模式挖掘，并派生出特征树、API 聚类与语义标签等"特征"类产物。本报告评估两个问题：**当前项目文档是否存在完整的特征挖掘功能描述，以及应当如何补充**；并结合外部项目与论文成果，给出可借鉴设计与功能补充方案。

评估范围为仓库全部文档（docs/ 下约 70 篇）与源码中的挖掘模块（src/libref/mining/、src/libref/services/code/）的对照。拉取环节说明：按任务要求经 gh-proxy.com 代理拉取时，git 与归档下载路径被该站点防护规则统一拒绝（HTTP 403，响应体为 "Access to proxy rules is forbidden on this hostname: gh-proxy.com"），页面访问正常；因此改由 GitHub 直连拉取同一仓库（提交 bdc0373）完成分析，内容与代理拉取一致。

## 二、现状盘点：文档中的特征挖掘功能

### 2.1 "特征挖掘"在 libref 中的含义

项目将"API 使用特征挖掘"定义为在调用实例上做统计与模式归纳的整体能力。docs/analysis/api-usage-mining-redesign.md（《API 使用特征挖掘改造设计》）把挖掘对象明确为四类派生产物（docs/features/db/derived.md）：

| 派生表 | 内容 | 构建方式（文档记载） |
|---|---|---|
| seq_patterns | 闭合调用序列模式 + 支持度 | 按样本分组后 PrefixSpan 闭合挖掘 |
| feature_nodes | 特征树：path/parent/depth/kind + calls/scopes | 按符号表父级挂载，调用数沿父链累加 |
| api_clusters | API 分组：成员/簇号/代表/规模 | 按作用域集合做确定性层次合并 |
| pattern_labels | 模式语义标签（LLM 归纳） | 按支持度取前若干模式归纳命名 |

四表均按挖掘档案（mining_profiles）隔离，真源是调用实例（call_instances），审核冲突行不进入构建。

### 2.2 已具备的能力清单

| 能力 | 文档位置 | 代码位置 | 出口暴露 |
|---|---|---|---|
| 闭合序列模式挖掘 | code-indexing-chain.md §4 | mining/prefixspan.py | pattern_insight.patterns |
| 特征树构建与浏览 | code-indexing-chain.md §4（一段话）；derived.md（表结构） | mining/feature_tree.py | pattern_insight.tree |
| API 聚类 | 同上 | mining/cluster.py | pattern_insight.clusters |
| 频次/实参形态/共现读模型 | code-mining.md（SQL 级） | services/code/mining_read.py | api_insight 系列 |
| 模式语义归纳（LLM） | features/pattern-induction.md | mining/induction.py | pattern_insight.labels |
| 惯用片段合成与去重 | ref/codescholar.md（借鉴分析） | mining/idiom.py | pattern_insight.snippets |
| 挖掘感知检索加成 | code-indexing-chain.md §5 | mining/scoring.py | 检索重排（freq_alpha/cluster_alpha） |
| 语料角色/档案/样本体系 | analysis/api-usage-mining-redesign.md（已声明落地） | code_sources、mining_profiles 表 | 读模型按档案过滤 |

此外，docs/ref/llm-feature-induction.md 已给出关键路线结论：**LLM 只放在"归纳层"**（模式命名、特征体系建树、答案生成），结构事实的抽取留给 tree-sitter 静态挖掘，以保 SQLite 单一事实源的可复现性。

### 2.3 文档缺口分析

对照上述清单，"特征挖掘"并非缺失，但文档呈现**重序列模式、轻特征体系**的不均衡，具体缺口如下：

1. **缺独立的特征挖掘功能设计文档**。特征树与聚类的算法语义仅在 code-indexing-chain.md 各占一句，derived.md 只有表结构与 SQL；聚类阈值（cluster_threshold=0.15、cluster_min_size=2）只出现在配置清单。对比之下，序列模式有 analysis/plan 三篇改造文档，模式归纳有 pattern-induction.md 专篇——特征树/聚类没有对应的方法学文档（构建语义、参数依据、边界行为、失败模式）。
2. **特征维度单一**。feature_nodes 本质是"限定名归属链 + 频次"的结构统计，不是语义特征。EpiCoder 式语义维度（workflow、数据处理、错误处理等，见 ref/epicoder.md）在 llm-feature-induction.md 中列为"可以做的"，但没有任何设计文档将其落为特征维度体系。
3. **特征树与聚类无语义标注**。pattern_labels 只覆盖 seq_patterns；api_clusters 与 feature_nodes 的成员没有命名、解释或类别路径。llm-feature-induction.md 的"特征体系建树"路线未见后续设计。
4. **特征质量与有趣度度量薄弱**。支持度已改造为样本口径，但缺乏关联规则类度量（lift、conviction 等）与"代表性/多样性"质量门禁；ref/codescholar.md 提出的 reusability/diversity 指标只停留在 idiom.py 的 helper 与借鉴清单，未文档化为挖掘质量标准。
5. **跨版本/跨档案特征对比未设计**。redesign 的 DPDK/MTL 场景只提"两个档案并列展示频率、共现和序列模式差异"，没有特征漂移（新增/消失/显著变化）的系统设计。
6. **特征消费链路文档薄**。scoring.py 的支持度/聚类加成、问答材料中特征的参与方式（模式标签仅作展示增强、不参与排序）散落各处，无专节说明"特征如何影响检索与回答"。
7. **评估体系缺失**。检索侧有 MRR/Recall@k 门禁（module-layout.md 测试清单），特征挖掘侧只有 redesign §十的 differential 用例要求，没有 golden 特征评测集与质量回归基准。
8. **遗留问题未闭环**。参考项目标签缺少来源提示、易被误读为官方推荐（plan/api-usage-mining-real-repo-followup.md）；C/C++ 宏展开覆盖（plan/api-usage-mining-macro-coverage.md）与挖掘性能（issue/api-usage-mining-performance.md）均在案未结。

**结论**：文档层面特征挖掘"功能存在、体系不完整"——结构统计类特征已落地并有 schema 文档，语义特征、质量度量、演化对比、评估基准四块存在明确缺口，且缺少一篇以"特征挖掘"为对象的总纲设计文档。

## 三、外部项目与论文成果清单

### 3.1 API 用法模式挖掘谱系

| 名称 | 出处 | 简述 | 可借鉴点 |
|---|---|---|---|
| MAPO（Xie & Pei，2006/2009） | taoxie.cs.illinois.edu | 代码搜索获取片段→调用序列聚类→SPAM 频繁序列→IDE 推荐，API 用法挖掘开山流水线 | 聚类+序列挖掘两段式组织 |
| UP-Miner（Wang 等，MSR 2013） | taoxie.cs.illinois.edu/publications/msr13-upminer.pdf | BIDE 闭合频繁序列 + 两轮 complete-linkage 聚类（SeqSim 加权 n-gram 相似度），指标 succinctness/coverage | 二次聚类去冗余；成对质量指标 |
| PAM（Fowkes & Sutton，FSE 2016） | 10.1145/2950290.2950319 | 近无参数的概率化 API 模式挖掘，效果超 MAPO/UP-Miner | 概率化排序，免调参 |
| GROUMINER（Nguyen 等，2009） | prism.ucalgary.ca 综述 | 对象使用图（GROUM）+ 频繁子图挖掘 + 异常检测 | 图模式保留控制/数据依赖 |
| BigGroum（Mover 等，2018） | github.com/cuplv/FixrGraph | 频繁项集预聚类 + 频繁子图挖掘 + popular/anomalous/isolated 分类，规模达千级项目 | 两阶段挖掘：项集粗聚、子图细挖 |
| MUDetect / AUG（Amann 等，MSR 2019） | sven-amann.de/publications/mudetect-msr19.pdf | API Usage Graph：动作/数据/条件节点 + 控制数据流边；跨项目挖掘显著提升精度与召回 | 条件/守卫节点表示；跨项目语料收益实证 |
| ExampleCheck（Zhang，UCLA 2019） | web.cs.ucla.edu/~miryung/Publications/ZhangDissertation2019.pdf | 38 万 Java 项目挖掘调用顺序 + 守卫条件（谓词挖掘，SMT 归并逻辑等价谓词） | 守卫条件作为独立特征维度 |
| APISan | mzolfaghari.com 论文表 I | 四类用法模式：返回值、参数关系、API 因果、隐式前后置条件 | 特征维度分类学（超出调用序列） |
| TIKANGA / PRMiner / APIMiner / APEx / ErrDoc | 同上综述 | 操作前置条件模板、频繁项集规则、错误处理规格、错误路径约束 | 规格挖掘的维度划分 |
| SoAy（KDD 2025） | arxiv.org/pdf/2405.15165 | 以预构造 API 调用序列（solution）引导 LLM 生成调用代码，配 SoAyBench 评测 | "调用序列即方案"的消费形态 |
| KDRAR（ACM TOIS 2025） | dl.acm.org/doi/full/10.1145/3771772 | 兼容性知识转描述性知识 + 教师 LLM 蒸馏推理，输出可解释的 API 推荐 | 兼容性建模与解释性输出 |
| 《面向开源生态的软件数据挖掘技术研究综述》（软件学报，2018） | jos.org.cn/html/2018/8/5524.htm | 中文综述：API 示例挖掘、结构化语义索引、社区知识集成 | 国内引文脉络与术语对照 |

### 3.2 自动特征工程（方法论迁移）

| 名称 | 出处 | 简述 | 可借鉴点 |
|---|---|---|---|
| OpenFE（清华，ICML 2023） | arxiv.org/pdf/2211.12507 | 23 个算子扩张候选特征，feature boosting 估计增量性能，successive halving 两阶段剪枝；Kaggle 实测超 99% 参赛队伍 | expand-and-reduce：候选扩张+增量效用估计+粗到细剪枝 |
| Featuretools（DFS） | artificial-intelligence-wiki.com | 关系/时态数据上算子堆叠自动合成深层特征 | 分层算子堆叠生成候选 |
| tsfresh | tsfresh.readthedocs.io | 时序特征自动提取（800+），Benjamini-Yekutieli 多重检验过滤 | 特征显著性过滤流程 |
| OneBM / ExploreKit / Autofeat | builtin.com | 类型驱动算子、元学习候选排序、单位约束生成 | 类型驱动的特征算子设计 |
| H2O Driverless AI | artificial-intelligence-wiki.com | 遗传算法组合特征并自动生成特征解释 | 特征可解释性（解释文本随特征产出） |

### 3.3 代码特征表示与归纳（近期）

| 名称 | 出处 | 简述 | 可借鉴点 |
|---|---|---|---|
| EpiCoder（ICML 2025） | arxiv.org/pdf/2501.04694 | Feature Tree：LLM 层次聚类建特征树，深度/宽度演化扩张（5k→140k 特征），按子树采样生成代码数据 | 特征树演化、新特征频次=兄弟均值估计、温度重加权平衡分布 |
| RPG（ICLR 2026）/ RPG-Encoder（ICML 2026，微软亚洲研究院） | microsoft.com/research；arxiv.org/abs/2602.02084 | Repository Planning Graph：提升的语义特征 + 代码依赖的统一表示；增量拓扑演化使维护成本与仓库规模解耦 | 语义特征提升的表示法；增量演化适配版本漂移 |
| CodeScholar（arXiv:2312.15157） | github.com/tart-proj/codescholar | idiom 挖掘：SAST+神经引导图生长；reusability/density 收敛指标 | 片段还原、多 API 交集预筛、质量指标（libref 已部分借用） |

### 3.4 闭合序列模式算法对照

PrefixSpan（前缀投影，libref 现用蓝本）之外，CloSpan（Yan 2003）首提闭合序列但需维护候选集；**BIDE**（Wang & Han，TKDE 2007）用双向扩展闭合检测与 BackScan 剪枝免候选维护，内存量级优于 CloSpan，UP-Miner 即采用 BIDE（hanj.cs.illinois.edu/pdf/tkde07_wangj.pdf）。SPAM（垂直位图，MAPO 所用）适合密集数据。libref 的自研 PrefixSpan 已支持 closed 输出，BIDE 的免候选维护思想可作为大语料下的备选优化。

## 四、可参考设计整理

将外部成果映射到 libref 的落点（优先级 P0 最高）：

| 设计点 | 来源 | libref 落点 | 优先级 |
|---|---|---|---|
| 特征维度分类学（返回值/参数关系/因果/前后置条件/错误处理） | APISan、APEx/ErrDoc 等 | 扩展特征模型为多维体系（§5.2） | P0 |
| 条件/守卫节点进入用法表示 | MUDetect AUG、ExampleCheck | 轻量守卫特征：检查-使用、清理配对、try 结构标签 | P1 |
| 二次聚类去冗余 + 成对质量指标 | UP-Miner（SeqSim/complete-linkage、succinctness/coverage） | 模式与片段的去冗余、代表性选择 | P1 |
| expand-and-reduce 特征选择 | OpenFE；tsfresh 显著性过滤 | 特征选择管线：候选扩张→增量效用/显著性→top-N 剪枝 | P0 |
| 层级频次聚合 + top-N 剪枝 + 树/频次分离 | EpiCoder（frequency.py 已借用一部分） | 深化 feature_nodes 统计口径与剪枝配置 | P0 |
| 特征树演化与兄弟均值频次估计 | EpiCoder | 特征体系跨版本演化机制 | P2 |
| 提升语义特征 + 增量拓扑演化 | RPG-Encoder | 特征漂移（版本对比）设计参照 | P2 |
| reusability/diversity 质量门禁 | CodeScholar（idiom.py 已有 helper） | 文档化为挖掘产物质量指标 | P0 |
| 兼容性知识与解释性输出 | KDRAR | 关联接口的兼容性/调用顺序解释 | P2 |
| 调用序列作为"方案"供问答消费 | SoAy | 模式标签进问答材料的强化形态 | P1 |
| 概率化免调参排序 | PAM | 支持度/排序口径的稳健化 | P2 |
| 算子堆叠候选生成 | Featuretools | 实参形态特征的组合算子 | P1 |
| 特征解释文本 | H2O Driverless AI | 特征/簇的 LLM 解释（复用 induction 网关） | P1 |
| 免候选维护闭合挖掘 | BIDE | 大语料序列挖掘的备选算法 | P2 |

## 五、特征挖掘功能补充设计方案

### 5.1 设计目标与硬约束

目标：把"特征挖掘"从单一结构频次统计升级为**多维特征体系 + 可评估 + 可演化**的完整能力，并补齐总纲设计文档。硬约束沿用项目既定架构（AGENTS.md）：mining/ 保持纯内存算法、零 db 依赖；派生数据按挖掘档案隔离、SQLite 为唯一真源；LLM 只作用于归纳层且产物单独存表、可整体重建；CLI/MCP/Web 三出口只经 services/ 读写。

### 5.2 特征模型扩展：四层特征体系

| 层 | 名称 | 特征键示例 | 来源 | 属性 |
|---|---|---|---|---|
| F1 | 结构统计特征（现有） | 归属链路径、calls、scopes | feature_tree.py 逻辑 | 静态、零幻觉 |
| F2 | 用法形态特征（补充） | 实参形态分布、调用形态（relation_kind）、调用序位、作用域类型 | call_instances 现有列聚合 | 静态 |
| F3 | 约束与守卫特征（补充） | 检查-使用配对、清理配对（如 open/close）、try/catch 包裹、返回值检查 | tree-sitter 轻量结构标签 | 静态，近似标注 |
| F4 | 语义归纳特征（LLM 层） | 类别路径、命名、工作流描述 | induction.py 网关扩展 | 可重建，不入真源统计 |

F1/F2/F3 为静态事实，进入真源派生表；F4 严格遵循 llm-feature-induction.md 结论——只对聚合后的特征/模式集合归纳，不逐文件调用 LLM。

### 5.3 数据模型变更（迁移草案）

1. 新表 `feature_facets`：维度（arg_shape/guard/call_shape 等）、取值、calls、scopes、profile_id，UNIQUE(profile_id, facet, value)——承载 F2/F3，不动 feature_nodes 结构。
2. 新表 `cluster_labels`：复用 pattern_labels 形制（幂等键=profile+cluster 哈希+model+prompt_version），补聚类与特征树节点的语义命名。
3. `feature_metrics` 视图化（不落库）：按需计算 lift、覆盖率、reusability/diversity，读模型透出。
4. 版本对比不做新表：读模型按两档案/两版本并行加载派生表做集合差与分布对比，产出漂移清单（新增/消失/支持度显著变化 top-N）。

### 5.4 算法与管线

1. **特征抽取**（mining/features.py 新模块，纯内存）：从调用实例流式聚合 F1–F3；守卫特征先做语法级轻量标签（if/try/catch/返回值检查），不做数据流分析。
2. **特征选择**（borrow OpenFE/tsfresh 思想）：候选扩张（F2 值组合算子，如"实参形态×调用形态"）→ 显著性/增量效用过滤（以对模式支持度或检索加成的边际贡献为准）→ 每层 top-N 剪枝（沿用 EpiCoder prune_to_top_n 配置形制）。
3. **特征评估**：每特征记录支持样本数、覆盖率、lift（相对独立性）；产物级记录 reusability（平均复用度）与 diversity（簇数）作为停机/截断与健康信号。
4. **语义归纳扩展**：把"特征体系建树"落为 F4 任务——输入为聚合后的特征键与簇成员（无源码原文），输出类别路径与命名，走 induction.py 同一网关、同一 pydantic 校验与幂等键形制；温度 0、断点续跑。
5. **演化对比**：同库跨版本或跨档案并列加载，输出特征漂移清单；参考 RPG-Encoder 的增量演化思想，只重算指纹变化部分（与 ast_cache 增量形制一致）。

### 5.5 消费面

1. **MCP/CLI**：pattern_insight 增加 `facets` 视图与 `feature_summary`；api_insight 的特征输出附维度标签；`profile` 参数沿用。
2. **检索**：scoring.py 在既有 freq_alpha/cluster_alpha 外增加特征加成通道（F4 标签命中加权），仍只作用于融合后头部。
3. **问答**：F4 类别路径与命名作为展示增强材料（同 pattern_labels 现行策略，不参与排序与引用定位），并按 real-repo-followup.md 要求带来源角色提示。
4. **Web**：七视图外增加特征维度浏览与版本对比页（对齐 frontend/pages.md 视图形制）。

### 5.6 质量与评估

1. **golden 特征集**：人工标注小规模语料（官方示例为主）的特征维度真值，作为回归基准。
2. **differential 用例**：改造前后特征分布对比（redesign §十已要求的"内部实现污染前后"差异用例扩展到特征维度）。
3. **质量门禁**：reusability/diversity 阈值 + 特征选择前后检索 MRR/Recall@k 不回退（复用 tests/test_retriever.py 门禁）。
4. 挖掘逻辑变更必须带 golden 或 differential 测试（AGENTS.md 既有要求）。

### 5.7 实施优先级

| 档 | 内容 | 粗估 |
|---|---|---|
| P0 | 总纲文档 docs/features/feature-mining.md；F2 用法形态特征 + feature_facets 迁移；reusability/diversity 文档化与健康透出；golden 小集 | 1–2 周 |
| P1 | F3 守卫特征轻量版；特征选择管线；cluster_labels/特征树命名（LLM 归纳层）；facets 消费面（MCP/CLI/Web） | 2–4 周 |
| P2 | 特征漂移对比；特征树演化；兼容性解释；BIDE 备选；APISan 式深层规格特征 | 按需排期 |

### 5.8 文档补充清单

1. **新增** docs/features/feature-mining.md：特征挖掘总纲（四层特征模型、算法语义、参数依据、质量指标）。
2. **新增** docs/features/db/feature-facets.md：新表 schema 与读写 SQL（对齐 derived.md 形制）。
3. **新增** docs/analysis/feature-evolution.md：跨版本/档案特征漂移设计。
4. **修订** docs/architecture/code-indexing-chain.md §4：特征树/聚类段落扩为算法语义摘要并指向总纲。
5. **修订** docs/features/pattern-induction.md：纳入特征/聚类标签归纳（或指向总纲 F4 节）。
6. **修订** docs/app/mcp.md 与 architecture/mcp-tools.md：facets/feature_summary 工具契约。
7. **闭环** real-repo-followup.md 的标签来源提示项，纳入 F4 输出字段（来源角色、非官方推荐标记）。

## 六、风险与边界

1. LLM 归纳存在非确定性：以温度 0、幂等键、可重建存表对冲（llm-feature-induction.md 工程约束）。
2. 特征组合爆炸：选择管线必须有每层 top-N 与候选上限预算（config 显式配置）。
3. 守卫特征为语法级近似，不能替代数据流分析；C/C++ 宏展开覆盖不足时须标注覆盖率（macro-coverage.md 在案）。
4. 参考项目语料带场景偏置，特征分布只能作场景档案解释，不自动代表官方推荐（redesign §十二）。
5. 本方案所有数字与结论均可追溯至仓库文档或 §三列出的外部出处；libref 未实现项已在文中明确标注为"补充/草案"。

## 参考资料

1. libref 仓库文档：AGENTS.md、docs/req/init-req.md、docs/analysis/api-usage-mining-redesign.md、docs/features/db/{overview,code-mining,derived}.md、docs/features/pattern-induction.md、docs/architecture/{code-indexing-chain,module-layout,mcp-tools}.md、docs/ref/{overview,epicoder,codescholar,llm-feature-induction}.md、docs/plan/api-usage-mining-real-repo-followup.md。
2. Wang et al. UP-Miner: Mining Usage Patterns from Source Code, MSR 2013.
3. Fowkes & Sutton. Parameter-Free Probabilistic API Mining across GitHub, FSE 2016.
4. Amann et al. Investigating Next Steps in Static API-Misuse Detection, MSR 2019.
5. Wang & Han. Frequent Closed Sequence Mining without Candidate Maintenance, TKDE 2007.
6. Zhang et al. OpenFE: Automated Feature Generation beyond Expert-level Performance, ICML 2023.
7. Wang et al. EpiCoder: Encompassing Diversity and Complexity in Code Generation, ICML 2025.
8. Yin et al. Closing the Loop: Universal Repository Representation with RPG-Encoder, ICML 2026.
9. Wang et al. SoAy: A Solution-based LLM API-using Methodology, KDD 2025.
10. Qi et al. Knowledge-Driven Reasoning for Compatible and Interpretable API Recommendation, ACM TOIS 2025.
11. Zhang D. Leveraging Program Commonalities and Variations for Systematic Software Development and Maintenance, UCLA PhD Thesis 2019.
12. 软件学报. 面向开源生态的软件数据挖掘技术研究综述, 2018.
