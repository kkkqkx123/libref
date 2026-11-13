# 检索问答基准（真实下载夹具）

本目录用真实使用场景做检索问答基准：文档来自第三方文档站，源码来自该库的官方仓库，
两者都经生产获取工具下载，而不是用本仓库自己的文件凑语料。

- **获取**：`fetch.py` 用生产抓取服务（robots/sitemap 发现、逐主机退避、正文抽取、快照留存）
  抓文档，用生产仓库管理（dulwich 浅克隆）拉源码，结果落在 `fixtures/`。
- **构建**：`build.py` 离线回放下载物，走生产净化、双泳道分块、落库、解析与挖掘。
- **不入库**：`fixtures/`（下载物与克隆）、`data/`（数据集）、`outputs/`（报告）全部忽略；
  只有 `bench.json`、脚本与判定集进版本库。

夹具规格见 `bench.json`：当前为 `httpx` 官方文档站（`https://www.python-httpx.org/`，
sitemap 23 页）与 `https://github.com/encode/httpx`（`httpx/` 子目录），
同一个库的文档与源码构成真实的库知识库场景。

## 运行

```bash
uv run python -m tests.e2e.bench.fetch             # 下载夹具（联网，一次即可）
uv run python -m tests.e2e.bench.build             # 离线回放，产出 data/bench.db
uv run python -m tests.e2e.bench.evaluate          # 方法矩阵 + 专项探针 + 门禁
uv run python -m tests.e2e.bench.evaluate --real   # 真实嵌入与重排，门禁含融合与精排
uv run python -m tests.e2e.bench.sweep [--real]    # 参数扫描
LIBREF_RUN_BENCH=1 uv run pytest tests/integration/test_retrieval_bench.py -q
```

`fetch.py` 支持 `--docs-only` / `--code-only` / `--refresh`；已下载时直接复用
`fixtures/lock.json` 记录的下载物。网络或站点不可用退出码为 2，标记环境不可用，不算回归。
`evaluate` 退出码非零表示门禁不满足；报告里同时列出失败项与全部观测值。
`fixtures/` 缺失时构建直接报错并提示先运行 `fetch.py`，门禁用例按环境不可用跳过。

## 文件职责

| 文件 | 职责 |
|---|---|
| `bench.json` | 夹具规格：文档站入口与抓取上限、源码仓库与子目录 |
| `fetch.py` | 下载：生产抓取服务抓文档并导出原始正文缓存，生产仓库管理克隆源码，写 `fixtures/lock.json` |
| `build.py` | 离线回放：页面缓存 → 净化分块落库，克隆目录 → 解析挖掘，产出数据集与清单 |
| `judgments.json` | 判定集：查询、类型（qualified/semantic/cjk/absent）、期望目标（文档站绝对地址或源码文件） |
| `evaluate.py` | 方法矩阵、专项探针、门禁检查与报告生成 |
| `metrics.py` | 纯排序指标：Recall@k、MRR@k、nDCG@k、空结果率、重复率、分布统计 |
| `fakes.py` | 两种向量与重排端点：确定性离线替身，以及读环境文件的真实模型（`--real`） |
| `gates.json` | 版本化门禁：全文路绝对下限、全方法结构不变量、真实模型下的融合与精排下限 |
| `sweep.py` | 参数扫描：检索期旋钮复用数据集，索引期分块旋钮各自重建数据集 |

## 方法矩阵与门禁口径

- `precise`：生产全文单路，代表性路径，两种模式下都进入绝对下限门禁。
- `fused` / `fused_rerank`：生产融合与重排接线。默认跑确定性替身，只受结构不变量约束
  （结果不得出现重复材料）；加 `--real` 时改用 `BAAI/bge-m3` 与 `BAAI/bge-reranker-v2-m3`
  （密钥取自仓库根环境文件，缺失即报错），此时融合与精排一并进入 `real_floors` 下限。

## 扩展

- **换夹具**：改 `bench.json` 的 `docs.start_url` 与 `code.repo_url`，重跑 `fetch.py --refresh`，
  然后按新语料重写判定集与门禁下限。夹具是下载物，不进版本库。
- **判定集**：`targets` 用绝对 URL（文档）或仓库内相对文件路径（源码），可给多个等价目标；
  `heading` 用标题子串进一步收窄文档命中。`absent` 类型用于观察空结果行为。
- **真实模型运行**：`--real` 直接调用生产传输层，不落快照文件；每次运行都会重新编码语料
  （文档 202 块、源码 1145 块，约两分钟）。
