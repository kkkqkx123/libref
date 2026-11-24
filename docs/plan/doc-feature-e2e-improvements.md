# 文档特征端到端评测改进方案

依据：`docs/tests/doc-feature-e2e-review.md` 与离线夹具、冻结归档两轮实测结果。本文只描述改动意图、责任落点与验收口径，不在代码中引用本文结构编号。

## 一、实测结论

离线确定性层按手算场景运行，`SUMMARY.txt` 的不变量全部通过：块 facet、AST 调用边、子序列覆盖、漂移判定、跨版本变化与加权前后排名均能产出可读报告。该层证明的是派生链路可运行，不证明真实语料上的效果。

冻结归档层完成了 httpx、jsoup、fiber 三对文档/源码夹具的回放。流程能跑通，但结果不支持冻结门禁：归档题目只有 2/2/1 条，锚点有 3 条解析异常；没有语言标记代码块的站点完全没有 AST 边；有代码块的 fiber 也以 0.4 置信度的名称模糊边为主。加权前后 nDCG/MRR 差分均为零，因此继续维持 `doc_feature_alpha=0` 是唯一有数据支撑的结论。

## 二、需要修复的实现问题

### 派生刷新丢失生态身份

`mine_patterns` 用调用方给出的 `ecosystem` 解析版本，但刷新文档派生时没有把该值传下去，派生函数回落到默认 `custom` 库。实测在 fiber 回放中新建了空的 `fiber/custom` 库与重复版本，真正的文档版本没有从这条路径收到覆盖结果。修复方式是让文档派生刷新显式接收并透传 `ecosystem`，并用集成用例断言一次 mining 刷新不会新增库或版本行。

### 文档版本缺少源码实体

归档把文档与源码索引在不同版本行，文档版本自身没有 `api_entities`。评测器为了让对齐可运行，临时从源码版本复制了实体。生产链路缺少同样的投影机制，因此真实文档片段没有目标实体可解析。短期由评测器显式声明该复制是回放前置条件；长期需要在 services 层确定实体投影或版本绑定口径，不能让 `custom` 默认库悄悄顶替。

### AST 证据在真实语料中占比过低

fiber 的片段调用绝大多数是 `not_target`/`AMBIGUOUS`，最终 AST 精确边只有 24 条，名称模糊边接近三万条。检索加权目前把所有对齐边等权计入，容易把模糊文本命中当成溯源质量。修复方式是按匹配证据分档：AST 符号边权重最高，限定名命中其次，名称模糊只保留很小的正向贡献；同时保留四视图里的原始边，不删除弱证据。

### 锚点解析与判定集身份

评测器只检查 URL 的第一个块，导致实际存在于其他标题路径的锚点被误报未解析；判定集里还写了一个不存在的实体名 `httpx.Timeout`，正确名称来自源码分支的 `httpx._config.Timeout`。修复方式是遍历同一页面所有块解析标题锚点，并更正该实体名。源码路径只用于候选符号，不能替代人工确认的文档锚点。

### 过滤有效性缺失

文档要求记录仅示例、排除废弃、仅对齐三类过滤后的命中率与结果数，现有输出没有这项。离线渲染器与归档评测都要在加权前后共用的检索结果上计算过滤统计，作为独立观测写入报告；不把过滤结果写成门禁。

## 三、分阶段落地

本轮落地上述五项修复：生态透传及回归用例、锚点全块解析、判定集实体名更正、过滤统计输出、加权证据分档。每一处涉及派生或排序的改动都配可手算的断言，完整测试仍需通过。

后续需要真实语料侧投入的改动单独推进，不在本轮以启发式补丁替代：

- 判定集扩充：生成器与子集命中评分已落地，人工确认与 dev/test 分栏仍是数据工作；当前样本量下配对区间退化为零，无法标定下限。
- 版本绑定：`entity_source_bindings` 已把文档版本显式绑定到源码版本，`usage_ingest` 与文档派生从源码版本读取实体，文档版本不再复制实体行。多版本文档的绑定身份仍需人工维护。
- 别名上下文：围栏与 HTML 代码块保留 import/using 上下文并参与别名解析；仍需人工抽检 Go/Java 片段的目标解析率，在此之前不把实体对齐率写入门禁。
- 漂移与结构化 facet：已加入漂移样本报告骨架，`param_names` / `return_hint` 改为签名或表格来源的结构化值；误报率需要人工标签样本后才能计算。
- 无答案查询的答案性判定已用有界覆盖集中度扣减消除当前误判；单纯按候选池词频加权会显著提高误弃答率，差分结论见 `docs/plan/bench-followup-fixes.md` 与 `docs/plan/benchmark-failures-analysis.md`。
- 给文档获取配方增加排除规则，挡掉 Read the Docs 的 `_sources/*.md.txt` 原始副本；给 NuGet/Go 清单身份告警补大小写归一与短名映射。

## 四、验收

- 集成测试断言 mining refresh 不新增 library/version，且派生仍落在原版本。
- 离线报告包含过滤统计；归档报告的锚点异常只保留真实问题。
- 加权证据分档有黄金值测试，弱证据不再与 AST 边同权。
- 默认测试、lint、类型检查通过；归档回放可重复且报告记录归档修订。
- 不改 `gates.json`：`doc_feature_alpha` 保持默认 0，直到判定集规模与漂移抽检满足标定条件。

## 五、真实夹具扩展

推迟项需要的不是再写一套采集逻辑，而是把 bench 规格里尚未获取的文档/源码对补齐。按用途选定如下：

| 推迟项 | 需要的真实项目 | 选型理由 |
|---|---|---|
| 扩大判定集与 dev/test 分栏 | 全部九个主夹具对：httpx、zod、jsoup、fiber、vue、pydantic、FluentValidation、Rocket、gradle | 覆盖 pypi/npm/maven/go/nuget/crates 六个生态与 Python/TypeScript/Java/Go/C#/Rust/Kotlin/Groovy 语言机制，每对站点与官方仓库共享术语，可继承现有判定锚点 |
| 实体投影与版本绑定 | Rocket v0.5/v0.5.2、pydantic latest/v2.14.3、FluentValidation latest/12.2.0、gradle current/v9.9.1、zod unversioned/v4.6.7 | 同时包含声明版本的站点与不声明版本的站点，用来检验文档版本键如何映射到源码标签，而不是把两套版本暗中合并 |
| 别名/导入上下文 | fiber(Go)、jsoup(Java)、gradle(Java/Kotlin/Groovy)、zod 与 vue(TypeScript)、Rocket(Rust)、FluentValidation(C#)、pydantic(Python) | 这些站点的示例覆盖包别名、桶重导出、属性宏、表达式树与装饰器；fiber 已有语言标记围栏，jsoup 没有，正好构成对照 |
| 漂移误报抽检 | fiber `/whats_new`、Rocket v0.5 指南、gradle current 用户手册、pydantic latest 迁移页、FluentValidation latest 文档 | 这些正文含发布变更、废弃提示或版本迁移，可抽取真正的漂移样本而不是靠通用词猜 |
| 结构化参数与返回 | pydantic、zod、httpx `/api/`、jsoup cookbook、gradle | 参数表、返回值小节与围栏交替出现，能检验 facet 是关键词候选还是可判定字段 |
| 候选噪声与跨库消歧 | 六个干扰池源码夹具：boltons、mitt、gson、mysql、once_cell、Dapper | 提供同生态近义名称，使泛词候选在无答案与全局查询里可被识别 |

本轮用现有 `tests.e2e.bench.fetch` 通过 7890 代理完成获取，24 个夹具全部生成归档并通过 `archive.verify`：十八个主夹具加六个干扰池。除 `httpx/code-head` 外，其余代码夹具修订与门禁记录一致；文档夹具及该源码夹具因上游变化产生新修订，门禁文件未改。为容纳当前站点规模，仅调整获取预算与版本身份：zod 80 页、pydantic 120 页、vue 120 页、FluentValidation 80 页、gradle 300 页；Rocket 文档版本改为站点声明的 `v0.5`；fiber 源码构建预算按本机实测放宽到 45000 毫秒。这些字段不进入载荷配方，改变的是获取口径，不是评测方法。

归档已就绪，完整数据集也已离线重建并回放；后续判定集扩充与跨版本对齐应基于这些修订重放，并重新记录观测值。

## 六、实跑结果与问题清单

本轮实际执行了全量单测、24 个夹具的离线建库、文档特征归档评测、检索矩阵评测与离线特征渲染。结果如下：

- `uv run pytest -q`：`910 passed, 36 skipped`，无失败。
- `python -m tests.e2e.bench.build`：退出码 0，24 个夹具全部通过身份、配方与摘要校验；数据集重建成功。
- `python -m tests.e2e.bench.evaluate_doc_features`：退出码 0；httpx/jsoup/fiber 的派生与判定链路可跑通，`invariants: PASS` 的离线渲染同时通过。
- `python -m tests.e2e.bench.evaluate`：退出码 1，共 14 项失败；`gates.json` 未改。

按影响排序，发现的问题为：

1. **门禁绑定全部过期（10 项）**。9 个文档夹具加 `httpx/code-head` 的归档修订与门禁记录不一致；其余 8 个源码夹具修订仍一致。这是重新获取后的预期结果，不能通过降低阈值绕过，需要在新修订上重新测量下限。
2. **无答案弃答率回归（4 项）**。`zod/docs` 的 precise/fused/fused_rerank 三路 test 弃答率为 0.500，低于 0.600；`pydantic/docs/fused_rerank` 为 0.250，低于 0.350。诊断显示不是召回问题：无答案查询仍返回 3–5 条结果，命中页多为 `zod.dev/ecosystem`、`v4/versioning`、`pydantic_settings` 等通用词页面，词面覆盖被判成 `weak` 而非 `none`。根因在 `store/evidence.py` 的答案性启发式对扩 corpus 后通用词重叠过于敏感，而不是排序链路本身。该项同时受修订过期影响，但即使重测下限也应先修判定逻辑。
3. **FluentValidation 文档混入原始源码副本**。当前归档 61 页里有 30 页来自 Sphinx 的 `_sources/*.md.txt`，与 30 页渲染 HTML 内容重复；它们贡献 402/799 个文档块，约一半。判定目标仍指向 HTML，因此重复率报表为 0，但原始副本会占据 top-k 名额并抬高词面重叠。获取规格只有包含前缀、没有排除规则，需要在不改动载荷摘要契约的前提下增加排除项并重新获取。
4. **清单身份告警（4 项）**。fiber/go 与 mysql/go 声明的是 Go 模块导入路径，FluentValidation/nuget 与 Dapper/nuget 是 NuGet 包名大小写差异；`repo_persist._check_manifest_identity` 仅做精确相等比较并只告警。库表没有产生重复实体，构建不中断；NuGet 分支应按大小写不敏感比较，Go 分支需显式记录短名与导入路径的映射，而不是当成身份漂移。
5. **文档特征回放仍只有三个夹具**。httpx/jsoup/fiber 有判定，新增的六套文档夹具没有人工确认的题目与锚点，因此本轮无法评价它们的实体对齐/漂移质量；fiber 的 30149 条链接仍以弱证据为主，不能据此标定 `doc_feature_alpha`。
6. **成本文档过时**。`tests/e2e/bench/README.md` 的规模表列的是旧站点快照数字，且表头列数与内容不一致；已按本轮实测更新页/块/实体/耗时，并修正表头。fiber 文档建库 298 秒仍是主要成本，文档特征回放单它一项约 367 秒。

上述 1–5 项都是下一轮判定集与门禁标定前必须处理的输入缺陷；本轮不改 `gates.json`，也不把环境性失败记为检索实现回归。
