# 端到端目录纪律

本目录只放需要真实网络的验证，例如拉取第三方仓库、访问真实网站与注册表。
常规测试目录只允许模拟传输与本地回环。
本目录用例默认跳过，需显式开启才执行。
目标仓库固定提交，网络失败标记环境不可用，不视为回归。

## 运行

常规提交不跑本目录。显式开启并执行单次验证：

单次离线单元加端到端跳过确认与显式拉取均合并到一次命令内完成，输出先落盘再读。
目标清单与固定标签见 fixtures/manifest.json，报告记录提交标识以备漂移说明。

## 同步与审查

同步一次多次复用，审查输出对照源码人工确认：

同步脚本按清单把网络仓拉取到 repos/ 目录，已存在则复用。
审查导出脚本把逐文件解析与分段报告落到 outputs/{lang}/ 目录。
文档运行器打开快照开关，原始抓取体留存到 snapshots/ 目录，
净化文本由导出步骤重新生成到 outputs/docs/ 下，审查报告附对应索引。
断言文件只断言不变量，不写输出文件。

## 探查与发现核查

`probe-discovery/` 是探查与发现的人工核查目录：直接调用生产命令行入口跑真实用例并落盘。
离线探查用例复用 `bench` 归档源码包本地解包，无需联网；探查文档与发现用例需真实网络，
失败记为环境不可用。脚本、规格与报告进版本库，用法与核查点见该目录 `README.md`。

```bash
uv run python tests/e2e/probe-discovery/fetch_probe_discovery.py --skip-network
```

## MCP 工具核查

`render_mcp_outputs.py` 用确定性种子库调用全部 MCP 工具，把结构化返回落到 `outputs/mcp/`，一个工具一份文本。默认离线，联网探查需显式开启；判读说明见 `fixtures/mcp/README.md`。

解析与抓取审查输出的首行记录生成时间，仓库标签与提交标识沿用清单和索引记录。

## 用法语料核查

`render_usage_mining_outputs.py` 在临时库中生成实现、官方示例、文档代码块与参考项目语料，调用生产挖掘与读模型，把来源、档案、模式、证据、健康、标签和差异判定落到 `outputs/usage_mining/`。默认完全离线，说明见 `fixtures/usage_mining/README.md`。

```bash
uv run python tests/e2e/render_usage_mining_outputs.py
```

## 真实仓库用法核查

真实仓库用例使用固定 tag 的 DPDK 与 MTL，流程分为同步与离线回放两步：

```bash
uv run python tests/e2e/sync_usage_repos.py
uv run python tests/e2e/run_usage_mining_real.py
LIBREF_RUN_E2E=1 uv run pytest tests/e2e/test_usage_mining_real.py -q
```

人工核查报告由 `run_usage_mining_real.py` 输出到 `outputs/usage_mining_real/`；角色语料覆盖全部源码文件（DPDK `lib/` 与 `examples/`、MTL 全仓），版本兼容、解析覆盖差分、档案健康比较与可反推质量门槛各有独立文件。全量语料回放约数分钟，网络失败按环境不可用跳过，不视为回归。

## 文档特征核查

`render_doc_feature_outputs.py` 用版本化夹具在临时库里走通生产文档块派生、实体对齐、模式覆盖、一致性与检索加权，把逐项结果写到 `outputs/doc_features/`；默认离线，说明见 `fixtures/doc_features/README.md`。`generate_doc_feature_questions.py` 只从 bench 判定集产出候选题目，人工确认后的判定存于夹具目录。

```bash
uv run python tests/e2e/render_doc_feature_outputs.py
uv run pytest tests/integration/test_doc_feature_outputs.py -q
uv run python tests/e2e/generate_doc_feature_questions.py
```

冻结归档层的评测入口是 `bench/evaluate_doc_features.py`，需先显式下载并构建 bench 归档，归档缺失时按环境不可用跳过；报告落到 `bench/outputs/retrieval-bench/doc-features/`，不写门禁。

## 检索问答基准

`bench/` 是检索问答基准目录：用生产抓取服务与生产仓库管理真实获取第三方文档站与官方仓库
（`fetch.py`，只做一次），导出为不可变归档后丢弃临时获取层，再离线回放建库（`build.py`）、
跑方法矩阵、专项探针与门禁。归档、数据集与报告都不入库，只有脚本、规格、判定集与门禁进版本库；
设计与判据见 `docs/bench/`，脚本说明见 `bench/README.md`。门禁用例位于
`tests/integration/test_retrieval_bench.py`，需显式开启，归档缺失时按环境不可用跳过：

```bash
LIBREF_RUN_BENCH=1 uv run pytest tests/integration/test_retrieval_bench.py -q
```

## 性能基准

`perf/` 是耗时与计数基准目录：纯算法用确定性生成器，索引、挖掘、检索与出口用例复用 `bench/` 的冻结归档，在忽略目录中重建小型数据集。报告、机器本地基线与临时数据集都不入库；脚本、规格与说明进版本库。判据见 `docs/tests/performance-benchmark-design.md`，运行说明见 `perf/README.md`。

```bash
uv run python -m tests.e2e.perf.run --gear smoke
uv run python -m tests.e2e.perf.run --gear all --save-baseline
LIBREF_RUN_PERF=1 uv run pytest tests/e2e/perf/test_performance.py -q
```
