# 性能瓶颈优化后复测记录

本文记录 `docs/plan/performance-bottlenecks.md` 中结构性优化落地后的复测数据。首轮数据与判读口径见 `docs/archive/perf/README.md`，本轮不覆盖旧结论。

## 运行条件

| 项 | 值 |
|---|---|
| 采集时间 | 2026-11-24T03:32:41Z、03:36:27Z、03:38:11Z |
| 代码提交 | `9231e89` |
| Python | CPython 3.14.10 |
| 系统 | Linux 6.18.58.1-microsoft-standard-WSL2 x86_64 |
| CPU | Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz，5 逻辑核 |
| 设置 | simvec、关闭重排与问答；冻结夹具与首轮相同 |
| 命令 | `uv run python -m tests.e2e.perf.run --gear all --save-baseline`，随后两次不带保存参数运行；`LIBREF_RUN_PERF=1 uv run pytest tests/e2e/perf/test_performance.py -q` |

同轮完成 `uv run pytest -q`（`941 passed, 37 skipped`）、`uv run ruff check src tests`、`uv run ruff format --check src tests` 与 `uv run mypy src`，均无失败。

## 结构计数门禁

| 审计项 | 首轮 | 本轮 | 判定 |
|---|---:|---:|---|
| 一次 `mine_patterns` 的 `call_instances` 主过滤读取 | 4 | 1 | 通过，基准中已改为硬门禁 |
| `seq_patterns` 行数 | 33 | 33 | 不变 |
| `feature_nodes` 行数 | 423 | 423 | 不变 |
| `api_clusters` 行数 | 247 | 247 | 不变 |

模式、特征树与簇的行数逐项一致，说明读取次数下降没有改变派生输出。新增的集成测试也分别锁定单次扫描、批量兄弟查询计数、目标声明下推、纯用法来源不写源码分段，以及 `scope` 持久列的使用。

## 文档净化归因

`micro.html_clean` 对三页冻结文档（147107 字节）逐子阶段计时。首轮归因运行与去掉重复子树 CSS 查询后的运行如下，单位毫秒：

| 子阶段 | 归因前中位数 | 优化后中位数 | 说明 |
|---|---:|---:|---|
| `score_prune` | 27.5082 | 10.6624 / 13.0460 | 评分剪枝本身不是主要成本 |
| `empty_prune` | 937.4412 | 326.9565 / 370.9526 | 原实现对每个空节点重复发起 CSS 查询；改为一次子树遍历 |
| `markdown` | 283.8948 | 122.2652 / 151.4999 | 转换仍留作后续观察项 |
| `serialize` | 0.3321 | 0.3331 / 0.3736 | 不是瓶颈 |
| `cache_key` | 0.2466 | 0.2097 / 0.2939 | SHA1 不是瓶颈 |
| `metadata` / `absolutize` / `structure_links` / `noise_strip` | ≤ 1.69 | ≤ 1.41 | 均非主导 |

优化前后对 `httpx/docs` 与 `jsoup/docs` 共 47 个冻结页面做了差分比对，`markdown`、`cleaned_html`、正文链接、结构链接与元数据完全一致。文档质量门禁随完整测试套件通过。

## 计时复测

下表取本轮最后一次全档报告；同机基线是同轮第一次运行，因机器负载波动，只用于标记待复核，不作为改动收益的结论。

| 用例 | 变体 | 基线中位数 | 复核中位数 | 判定 |
|---|---|---:|---:|---|
| `pipeline.index` | total | 6682 | 7086 | tie |
| `pipeline.index` | stage=clean | 2059 | 2242.5 | tie |
| `pipeline.index` | stage=index | 2059 | 2327 | tie |
| `store.mining_scan` | mine_patterns | 1850.2589 | 2237.7222 | tie |
| `store.fts_scope` | single/library/global | 1.7969 / 1.7571 / 1.4479 | 1.2258 / 1.7486 / 1.5969 | 见全档报告 |
| `service.retrieval` | precise/fused/fused_rerank | 5.0342 / 5.6611 / 9.8605 | 6.0399 / 6.9737 / 9.4658 | 见全档报告 |
| `exit.web` | concurrency=1/4 | 119.8058 / 175.9044 | 88.1458 / 76.8414 | needs_review |

更稳定的 `corpus` 单轮复核中，`store.mining_scan` 中位数为 1259.9041ms（相对同轮基线判为 `improved`），`micro.html_clean/empty_prune` 为 326.9565ms（判为 `improved`），说明结构改动方向有效。全档运行中的 AST 冷启动、亚毫秒子阶段与 Web 尾点出现 `needs_review`，与首轮同类噪声一致；默认报告只标记，不拦截。所有结构审计均为硬通过，报告无 `failures`。

## 本轮未覆盖

- 真实模型编码、Qdrant 与真实 ASGI 服务器的容量用例仍按计划留在显式档，本轮没有外部后端数据。
- Worker 预热只作用于仓库索引任务；一次性 CLI 保持懒加载。`micro.ast` 冷/热数据不因该改动变化。
- 健康视图抽样在冻结小夹具上等于全量，抽样元数据随响应返回；大规模语料的分层效果仍需在容量档验证。
