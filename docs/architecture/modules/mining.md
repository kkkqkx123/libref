# mining — 统计与序列挖掘

## 职责

Branch B 的纯内存算法库：调用序列的模式挖掘（PrefixSpan）、频率/共现统计、API 聚类、特征树、调用切面、惯用法片段、语义归纳（LLM 前后处理）、挖掘感知的检索评分与文档对齐。全部为纯函数/纯类，持久化由 services 层编排，零 db 依赖。

## 关键文件

| 文件 | 职责 |
|---|---|
| `normalize.py` | 限定名 ↔ 整数双向映射（排序后赋连续索引，稳定可复现） |
| `frequency.py` | 精确/分层频率统计与 Top-N 截断（全模块唯一截断入口） |
| `cooccur.py` | scope 级共现矩阵 |
| `prefixspan.py` | 自研 PrefixSpan（含 BIDE 闭包判定、top-k 堆剪枝） |
| `cluster.py` | 并查集 API 聚类（Jaccard 重叠） |
| `feature_tree.py` | 所有权链聚合的特征树 |
| `facets.py` | 调用切面（参数形状/关系/顺序/守卫对）聚合与评分 |
| `idiom.py` | 掩码惯用法片段、证据去重、复用健康信号 |
| `induction.py` | 模式/聚类的 LLM 归纳：Prompt 组装与输出校验 |
| `scoring.py` | 挖掘感知的检索评分原语 |
| `doc_alignment.py` | 文档块 ↔ API 实体对齐、模式覆盖、一致性/漂移判定 |

## 核心算法

### PrefixSpan 序列模式挖掘（prefixspan.py）

移植自 extratools 的 `prefixspan.py + frequent.py + closed.py + topk.py`，定制默认长度窗口 `minlen=2, maxlen=10`（API 调用序列）。类型：`DB = list[list[int]]`（item 经 normalize 编码），支持度 = 包含模式的**序列数**（非出现次数）。

- **投影 `_next_entries`**：对每个 `(序列id, 模式末匹配位置)` 在其后缀中扫描，`item → [(id, pos)]` 且**每序列每 item 只记首个位置**——一次扫描同时完成扩展枚举与子模式匹配，并保证支持度只计一次。
- **BIDE 反向扫描 `_reverse_scan`**：对模式中每个前驱 item，从各匹配位置向前找；沿途收集经过的 item 集合做交集，非空即存在"可中间插入且支持度相同的超模式"→ 非闭。头/尾哨兵 `[None, *patt, None]` 覆盖头扩展与尾扩展。
- **闭包判定 `_is_closed`** 两步：反向插入检查 + 正向尾扩展检查（匹配位置之后的 item 集合交集为空才算闭）。**闭包剪枝 `_can_closed_prune`**：反向检查非闭时整棵子树剪掉。
- **`frequent(minsup, closed=True)`**：递归驱动，长度窗口内输出，`closed=True` 时以 `_is_closed` 过滤——services 挖的是**闭序列模式**（无同支持度超模式冗余）。
- **`topk(k)`**：小顶堆维护 top-k，动态阈值剪枝；扩展 item 按支持度上界降序排序，一旦上界低于阈值即 break。

### 频率与共现

- `prune_to_top_n(counts, n, max_depth=3)`：分层截断——先按点分深度分桶，深度超限丢弃，桶内按 `(-count, name)` 排序（稳定可复现）各取前 n。
- `top_exact_names`：唯一截断入口；分层计数仅用于展示。
- `cooccur_matrix`：`scope_key = f"{file}\0{caller}"` 分组去重后，组内所有二元组合计数（`sorted` 后组合保证键方向 `(a,b) ∧ a<b`）。

### 聚类（cluster.py）

`cluster_apis(scope_sets, threshold=0.15, min_size=2)`：统计各 API 的 scope 数；对共现对算 **Jaccard 重叠率** `inter / (occ[a]+occ[b]−inter)`，≥0.15 用**并查集**合并（union 时根取字典序较小者保证确定性）；连通分量为一组，<2 剔除，代表取 scope 数最多者，组间按 `(-size, members)` 排序赋 `cluster_id`。区分"共使用"与"可互换"由下游处理。

### 特征树与切面

- `build_feature_tree`：沿 `api_entities` 的 parent 链自根向叶累加调用次数与 scope 并集；无所有权数据时回退点分层切。节点输出 `{path, parent, depth, kind, calls, scopes}`。
- `aggregate_facets` 五维度：`arg_shape`（逐参掩码后的调用形状）、`call_shape`（`callee@relation`）、`call_order`（位置桶 first/middle/last，按占比 1/3、2/3 分界）、组合行（按 `(callee, 形状, relation)` 计数取前 budget 条）、`guard`——配对后缀（open/close、lock/unlock、begin/commit 等 13 组）与 check-then-use（`check/assert/verify` 开头的调用者对其后续调用）。
- 切面评分：`coverage = scopes/total_scopes`；`concentration = calls/facet_total`；**lift**（仅 arg_shape）= 观测占比 / (callee 占比 × 形状占比)，衡量该形状是否被该 callee 特异性偏好（1.0 为独立）。

### 惯用法与归纳

- `mask_arg`：参数掩码——字符串 `'...'`、数字 `0`、`True/False/None` 原样、关键字参数保留 key 形状、其余 `_`。
- `synthesize_snippet`：按命中索引组装可读片段，索引间隙插入 gap 行（记录跳过数与源码 span），命中/邻居调用输出 `callee(掩码参数)` + 行号 + 置信度。
- `deduplicate_evidence`：两级去重——每文件按 `(-命中数, 最差置信度, 文件, caller)` 取前 1；再全局按掩码签名跨文件去重。置信度排序 EXTRACTED < INFERRED < AMBIGUOUS。
- `reuse_diversity`：健康信号 `reusability = log(total_support/count)`、`diversity = log(len(clusters))`，检索侧据此封顶 alpha。
- `induction.py`：`build_label_prompt` 组装 system+user 两消息（scope、来源提示、模式、参数形状、签名、同侪、related_apis 白名单）；输出经 pydantic 模型（`PatternLabel`：name 1–80 字、category_path ≤4 层等）与**材料边界标记黑名单**（`<unit`、`<excerpt`、`<call` 等含闭合形式）双重校验——命中即拒绝，防止模型输出伪造检索材料。`pattern_hash = SHA1(JSON(pattern))` 是幂等 upsert 键。

### 检索评分原语（scoring.py）

- `support_for_entities`：取能命中任一实体的最大模式支持度，除以列表内最高支持度归一到 0..1。
- `cluster_hit`：内容/实体覆盖**同一簇 ≥2 个成员**（weak = 恰 1 个）。
- `capped_alpha(base_alpha, reusability, floor=0.02)`：语料可复用性弱时把挖掘加权封顶到 floor。

### 文档对齐（doc_alignment.py）

- 匹配等级：`symbol`(4) > `signature`(3) > `context_symbol`(2) > `name_fuzzy`(1)。
- `build_api_index`：exact（全名与 `signature:name`）、`short:` 短名、`norm:` 蛇形归一短名；一 key 多实体进入 ambiguous 桶，不强行指定。
- `align_documents`：snippet 调用按 URL 对块；绑定到代码（call_hash 命中块的 code_hashes）给 `symbol` @1.0/0.8；正文出现 exact 全名给 `signature` @0.8；fuzzy 唯一命中给 0.4；**ambiguous 用上下文父路径消歧——最高分不唯一即放弃，绝不臆造实体**。
- `cover_patterns`：块 `call_sequence` 保序包含模式 → `example_code`；模式各名词以 token 形式出现在正文 → `prose`；min_support 默认 2。
- `compare_consistency` 漂移判定：句级扫描弃用句（含弃用词、无否定词、无排除上下文、句中出现被跟踪实体）；`deprecated 元数据 + 弃用句` → **drift**；有模式覆盖 → **match**；有代码但无实体对齐 → drift；否则 unknown。证据以 SHA-256（sort_keys）存证。

## 对外接口

`PrefixSpan`、`build_table`/`encode_sequences`/`decode_pattern`、`top_exact_names`/`prune_to_top_n`、`cluster_apis`、`build_feature_tree`、`aggregate_facets`、`synthesize_snippet`/`deduplicate_evidence`/`reuse_diversity`、`build_label_prompt`/`parse_label_payload`、`build_api_index`/`align_documents`/`cover_patterns`/`compare_consistency`、`scope_key`、`support_for_entities`/`capped_alpha` 等。

## 依赖与约束

纯内存，无 db/网络依赖；输入为 plain-Python 记录（pydantic 的调用实例、声明、文档块投影）。确定性贯穿始终：所有排序带名字序决胜键，同一输入必得同一输出。截断契约集中：`top_exact_names` 与 `select_facets` 是各自维度的唯一保留/截断入口。
