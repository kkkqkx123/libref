# 性能基准目录

本目录用生产入口与冻结语料测量索引、挖掘、检索和出口链路的耗时与计数，不替代检索质量基准。设计与判据见 `docs/tests/performance-benchmark-design.md`。

## 运行

```bash
uv run python -m tests.e2e.perf.run --gear smoke
uv run python -m tests.e2e.perf.run --gear corpus --save-baseline
uv run python -m tests.e2e.perf.run --gear all --enforce
LIBREF_RUN_PERF=1 uv run pytest tests/e2e/perf/test_performance.py -q
```

`--gear smoke` 只跑纯算法小档；`corpus` 会重建三个小型冻结夹具并跑存储、挖掘与检索；`exit` 跑进程内 HTTP 并发；`all` 跑全部。归档缺失、生成器失败和数据规模不变量不成立都属于硬失败。

## 文件职责

| 文件 | 职责 |
|---|---|
| `perf.json` | 用例、规模、参数、预热与重复次数 |
| `harness.py` | 计时、统计、夹具生成、环境摘要、报告与基线比较；用例直接调用生产入口 |
| `run.py` | 按档位运行、保存基线、输出报告与退出码 |
| `test_performance.py` | 显式开启的报告门禁，不重复计时 |

## 产物

报告写在被忽略的 `outputs/{timestamp}/`，包含 `report.json` 与 `summary.md`；`outputs/latest.json` 指向最近一次报告。`outputs/dataset/` 只保存从冻结归档重建的临时数据集，可随时删除重建。`outputs/baseline.json` 是机器本地基线，不入库；跨机器不比较。

## 口径

- 中位数与 p95 按最近秩计算，样本数、变异系数和原始样本都落入报告。
- 计数审计与计时分离：挖掘扫描次数、查询计划探针和结构不变量在独立轮次统计。
- 基线噪声带取基线变异系数的两倍与 10%（p95 为 20%）中的较大值。默认只标记 `needs_review`；显式 `--enforce` 才让该标记影响退出码。
- `pipeline.index` 的每条记录是冻结夹具重建一次的结果，重复次数给出分布；`micro.ast/cold` 每轮清空解析器与查询缓存。
