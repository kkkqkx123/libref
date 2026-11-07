# EpiCoder 功能分析

> 仓库：https://github.com/microsoft/EpiCoder（本地 `ref/EpiCoder`）。MIT；ICML 2025 论文《EpiCoder: Encompassing Diversity and Complexity in Code Generation》官方代码，产出数据集 EpiCoder-func-380k。
> 定位：**特征树（Feature Tree）驱动的代码指令数据合成框架**，目标是生成 LLM 训练数据——不是检索知识库。

## 1. Pipeline 五阶段

1. **语料精简**（前置）：`cluster/main.py`——SentenceTransformer 嵌入 + k-Center Greedy（`cluster/utils/kcenter_greedy.py`）从 The Stack V2 jsonl 中选 core-set。
2. **特征抽取**：`extract/extract_features.py` → `extract_frequency_count.py` → `extract_separate_frequency.py`。
3. **特征演化**：`evol/feature_evol.py`、`evol/merge_expanded_features.py`。
4. **指令与代码生成**：`gen/gen_question.py`、`gen/gen_code.py`；Docker 沙箱调试 `debug/`。
5. **训练数据收集**：`utils/get_train_data.py`。

## 2. ⚠️ 关键事实：本仓库没有任何 AST 工具

全仓库 grep 无 `ast` / `tree-sitter` / `javalang`（`requirements.txt` 仅 azure/openai/datasets）。**代码 → "特征"完全靠 LLM**：

- `extract/extract_features.py::extract_single_feature`（L85）把源码填入 prompt 模板 `prompt/extract/prompt12.txt` 调 GPT-4，解析 `<begin>...<end>` 包裹的 JSON。
- prompt12 规定抽取 **21 类语义特征**（语言、workflow、实现风格、数据处理、计算操作、依赖库、算法、数据结构、错误处理等），输出嵌套 JSON（dict=维度，list=叶子特征）。
- 即：它抽取的是 **LLM 描述的语义特征，而非静态 AST 的 API 调用名/参数/类型**；没有调用图、签名、类型推断。

LLM 调用环节对照表：

| 环节 | 文件 | LLM？ |
|---|---|---|
| 语料嵌入 + k-center 选样 | `cluster/` | 否（本地嵌入） |
| 代码→语义特征 | `extract/extract_features.py` + prompt12 | **GPT-4** |
| 特征聚类成树 | `extract/construct_tree.py::get_tree` | **GPT-4** |
| 频次统计/剪枝/拆分 | `extract/extract_frequency_count.py`、`utils/tree.py` | 否 |
| 特征演化 | `evol/feature_evol.py` | GPT-4o |
| 指令/代码生成、调试 | `gen/`、`debug/` | LLM |

## 3. Feature Tree 数据结构与构建

- 节点结构：嵌套 JSON。dict 节点 = 粗粒度维度（如 `computation operation` → `mathematical operation`），list 节点 = 叶子特征（如 `"base conversion"`）。层级：语言无关大类 → 子维度 → 叶子。
- 局部树：每代码文件按 prompt12 的 21 类结构输出；全局树：`extract/construct_tree.py::get_tree`（L59）每步随机采 1000 特征让 GPT-4 聚类成树，迭代存 `all_tree_structures.json`。
- 树/频次工具库：`utils/tree.py`（500 行）——`remove_frequency`（带频次树→纯特征树）、`sample_feature_tree`（按层条件采子树控制复杂度）、`count_descendant`（L62，前缀统计后代数）、`calculate_all_frequency`。

## 4. 频次统计与剪枝（纯静态，可直接复用）

- `extract/extract_frequency_count.py::get_feature_frequency`（L41）遍历样本特征 JSON，`update_feature_frequency`（L5）递归累加——**按维度层级聚合叶子出现次数**，频次挂在叶子节点。
- 过滤：`skip_features = ["summary","purpose","issues & bugs","potential scenario"]`（L47，叙述类不计入）。
- 剪枝：`prune_to_top_n`（L67）每层按频次排序保留 top-N（实验 n=50、max_depth=3，子 dict 不足 3 时折叠为计数）。
- `extract_separate_frequency.py`：把"树结构"（`_fea.json`，无频次）与"频次表"（`_fre.json`）**分离存储**，并按语言白名单过滤维度。
- **没有共现（co-occurrence）挖掘**：只有各维度独立频次 + 嵌入级 k-center 选样；无跨特征共现矩阵、无序列挖掘。

## 5. 数据过滤/清洗逻辑

- 前置：Stack V2 自带 license permissive、`is_vendor/is_generated` 元数据过滤；`cluster/` 嵌入去重。
- 统计层：`skip_features` 剔除叙述特征；`max_depth=3`；`prune_to_top_n` 剪低频。
- 结果层：`utils/get_train_data.py::get_valid_indices`（L85）要求测试 success 非 null；`get_code_from_dir_without_test`（L53）**剔除文件名含 "test" 的文件**；Docker 沙箱执行 + 最多 5 轮 LLM 修复，保证样本可执行。
- 工程模式：`extract_features.py` 并发线程池 + jsonl 断点续跑（`utils/file_operation.py`）。

## 6. 对本课题的借鉴清单

**可直接复用（纯静态工程逻辑）：**
1. `extract_frequency_count.py` 的层级频次聚合 + top-N 剪枝 → 直接用于 API 维度频次统计。
2. `utils/tree.py` 的嵌套特征树操作库（采样/合并/统计）。
3. "结构树 / 频次表"分离存储的 schema 设计。
4. `cluster/` 的嵌入去重采样 → 语料精简与坏样本过滤。
5. 并发抽取 + 断点续跑的批处理工程模式。

**需改造为纯静态的环节（本课题做法）：**
- 用 AST 分析替换 `extract_features.py` 的 LLM 抽取：Python 用 `ast`、Java 用 javalang/tree-sitter，直接抽取 **API 调用名（Call 节点）、实参、类型注解、import 依赖、控制流上下文**；仍按 21 类维度类似的层级组织 JSON，从而无缝复用其频次统计与树构建代码。
- 用共现矩阵 + 层次聚类（或 FP-Growth）替换 `construct_tree.py` 的 LLM 聚类建树。
- 补一个共现挖掘模块：抽取层记录每个文件的 API 集合，倒排索引统计 (api_i, api_j) 共现次数；序列挖掘（PrefixSpan）则需另建。

## 7. 局限性（核实后确认）

目标是生成训练指令数据，不构建检索知识库；无检索引擎、无 Web 查询界面、无 MCP 服务；特征抽取依赖 LLM（本课题必须改为纯静态 AST 以免引入二次幻觉）；不挖掘频繁 API 调用序列（PrefixSpan/GSP 缺失）；无官方文档抓取；无人工审核流程。

## 论文相关工作表述

EpiCoder 提供开源的代码特征抽取框架，提出 Feature Tree 分层语义特征建模方法，并实现了层级频次统计与剪枝的工程化模块；但其特征抽取环节依赖大模型而非静态分析，系统目标面向训练数据生成，不构建检索知识库，缺少 API 调用序列挖掘能力，也没有 MCP 服务和面向开发者的 Web 查询入口。
