# 性能基准首轮实测记录

本文记录性能基准实现后的首轮实测数据与判读，数据来自本地报告，不写回机器本地基线。实现与判据见 `docs/tests/performance-benchmark-design.md`，运行入口见 `tests/e2e/perf/README.md`。

## 运行条件

| 项 | 值 |
|---|---|
| 采集时间 | 基线 2026-11-24T02:52:26Z，复核 2026-11-24T02:55:36Z |
| 代码提交 | `9a9e61f` |
| Python | CPython 3.14.10 |
| 系统 | Linux 6.18.58.1-microsoft-standard-WSL2 x86_64 |
| CPU | Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz，5 逻辑核 |
| 设置 | simvec、关闭重排与问答；分块与检索参数取默认值；Web 用例单独使用测试主体 |
| 归档修订 | `httpx/docs@a4dea3a01527`、`jsoup/docs@68fb6588ff0f`、`httpx/code-7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016@f01c86720683` |
| 数据集 | 三个冻结夹具重建的临时数据集；Web 单独记录时复用该数据集 |

命令依次为 `uv run python -m tests.e2e.perf.run --gear all --save-baseline`、同命令不带保存参数做复核，以及 `LIBREF_RUN_PERF=1 uv run pytest tests/e2e/perf/test_performance.py -q`。完整套件与静态检查同轮通过：`926 passed, 37 skipped`，ruff、mypy 均无问题。

## 判读口径

表内时间单位为毫秒。基线列是首次保存的机器本地基线，复核列是随后一次同机运行的同口径测量。变异系数是基线样本的标准差除以均值。判定按“同机、同档、同夹具、同设置”比较：中位数超过基线噪声带记为待复核，p95 尾部阈值单独计算。结构计数与查询计划属于硬审计；本轮无硬失败。

## 纯算法

| 用例 | 变体 | 基线中位数 | 基线 p95 | 复核中位数 | 复核 p95 | 判定 | 计数 |
|---|---|---:|---:|---:|---:|---|---|
| micro.prefixspan | n=64 | 90.2479 | 92.8660 | 87.9124 | 94.1645 | tie | patterns=983 |
| micro.prefixspan | n=256 | 327.0003 | 346.7390 | 314.9872 | 317.5374 | tie | patterns=3509 |
| micro.prefixspan | n=1024 | 1239.7824 | 1323.4706 | 1235.0862 | 1316.4134 | tie | patterns=12612 |
| micro.chunking | copies=1 | 1.8620 | 1.9980 | 1.9064 | 1.9290 | tie | chunks=8 |
| micro.chunking | copies=4 | 7.3427 | 10.0385 | 7.3372 | 7.4250 | tie | chunks=32 |
| micro.chunking | copies=16 | 29.9036 | 30.2208 | 29.9911 | 31.0241 | tie | chunks=128 |
| micro.ast | python/warm | 2.3356 | 2.4416 | 2.5791 | 2.8758 | needs_review | decl=5, call=5 |
| micro.ast | python/cold | 26.8636 | 27.2560 | 26.3503 | 26.4676 | tie | — |
| micro.ast | java/warm | 2.9307 | 3.5239 | 2.6499 | 2.9576 | tie | decl=9, call=4 |
| micro.ast | java/cold | 17.8730 | 18.2285 | 17.8447 | 19.2163 | tie | — |
| micro.ast | go/warm | 2.1972 | 2.3387 | 2.2136 | 2.3105 | tie | decl=7, call=3 |
| micro.ast | go/cold | 8.3918 | 8.8133 | 8.7119 | 8.8236 | tie | — |
| micro.ast | rust/warm | 2.6654 | 2.8180 | 2.6459 | 2.7656 | tie | decl=7, call=5 |
| micro.ast | rust/cold | 40.2272 | 40.4302 | 27.7801 | 30.9006 | improved | — |
| micro.ast | csharp/warm | 2.6115 | 2.6743 | 2.5206 | 2.6618 | tie | decl=5, call=5 |
| micro.ast | csharp/cold | 146.5598 | 150.0909 | 147.1262 | 151.5992 | tie | — |
| micro.ast | javascript/warm | 3.4894 | 3.8958 | 3.3512 | 3.7590 | tie | decl=8, call=7 |
| micro.ast | javascript/cold | 27.7176 | 28.0040 | 28.1465 | 33.9396 | needs_review | — |
| micro.ast | kotlin/warm | 2.9097 | 3.2363 | 2.8682 | 3.0180 | tie | decl=6, call=6 |
| micro.ast | kotlin/cold | 106.3335 | 122.7448 | 106.4452 | 107.3122 | tie | — |
| micro.ast | groovy/warm | 2.6591 | 2.7793 | 2.4186 | 2.4832 | tie | decl=6, call=6 |
| micro.ast | groovy/cold | 51.3335 | 52.0988 | 50.8546 | 50.8850 | tie | — |

分块耗时随副本数近似线性增长，围栏不变量在全部样本上成立。PrefixSpan 的有界模式数随规模增长，16 倍序列数对应约 13.7 倍耗时，未出现无界枚举。AST 的冷热差是缓存效应：热解析稳定在 2–3ms，冷解析从 Go 的约 8ms 到 C# 的约 147ms 不等。冷态样本只重复五次，个别尾点波动足以触发基于 p95 的待复核标记，不代表回归。

## 索引与挖掘

| 用例 | 变体 | 基线中位数 | 基线 p95 | 复核中位数 | 复核 p95 | 判定 | 计数 |
|---|---|---:|---:|---:|---:|---|---|
| pipeline.index | total | 4062 | 4466 | 4048 | 4531 | tie | 3 夹具 |
| pipeline.index | stage=clean | 1304.5 | 2272 | 1307.5 | 2266 | tie | — |
| pipeline.index | stage=split | 84.5 | 133 | 86 | 133 | tie | — |
| pipeline.index | stage=store | 75.5 | 124 | 85 | 108 | tie | — |
| pipeline.index | stage=commit | 0 | 0 | 0 | 0 | no_baseline | 亚毫秒，整数取整 |
| pipeline.index | stage=unpack | 8 | 12 | 7 | 21 | needs_review | — |
| pipeline.index | stage=index | 1096 | 1366 | 1087 | 1435 | tie | — |
| pipeline.index | stage=verify | 0 | 0 | 0 | 0 | no_baseline | 亚毫秒，整数取整 |
| pipeline.index | stage=mine | 0 | 0 | 0 | 0 | no_baseline | 亚毫秒，整数取整 |
| store.mining_scan | mine_patterns | 894.5546 | 914.7993 | 925.7970 | 944.5805 | tie | scans=4, patterns=33, nodes=423, clusters=247 |

三个夹具的三轮重建总耗时中位数约 4.1s。文档分支的净化阶段约占三分之一，分块与落库合计不足 0.2s；源码分支的解析与落库约 1.1s。源码夹具的挖掘阶段在整数毫秒粒度下接近零，但独立审计显示一次挖掘仍读取 `call_instances` 四次，与既有性能问题记录一致。该计数目前只作为观测，不在优化落地前写成硬门禁。

## 存储、检索与出口

| 用例 | 变体 | 基线中位数 | 基线 p95 | 复核中位数 | 复核 p95 | 判定 | 计数 |
|---|---|---:|---:|---:|---:|---|---|
| store.fts_scope | single | 1.0023 | 1.9213 | 0.9635 | 1.6061 | tie | 16 查询，首条命中 25 |
| store.fts_scope | library | 1.2993 | 2.3661 | 1.3117 | 2.3918 | tie | 16 查询，首条命中 25 |
| store.fts_scope | global | 1.1444 | 2.1505 | 1.1676 | 2.1795 | tie | 16 查询，首条命中 25 |
| store.fts_scope | audit | — | — | — | — | 硬审计通过 | 作用域索引探测命中，计划含 covering index |
| service.retrieval | precise | 4.3377 | 6.0690 | 3.8887 | 5.3674 | tie | 12 查询，首条返回 8 |
| service.retrieval | fused | 4.4929 | 6.2149 | 4.3554 | 5.9873 | tie | 12 查询，首条返回 8 |
| service.retrieval | fused_rerank | 6.0951 | 7.9682 | 5.8031 | 8.0162 | tie | 12 查询，首条返回 8 |
| exit.web | concurrency=1 | 46.6501 | 49.4395 | 46.0548 | 47.3641 | tie | 每批 8 请求，共 64 行 |
| exit.web | concurrency=4 | 50.4936 | 51.5937 | 45.0713 | 46.4268 | improved | 每批 8 请求，共 64 行 |

FTS 三种作用域都在毫秒级，查询计划确认作用域过滤使用 `idx_documents_version` 的覆盖索引探测。离线替身下三种检索预设的中位数依次为约 3.9ms、4.4ms、5.8ms，重排增加约 1.4ms。Web 用例在小数据集上每批 8 个请求约 46ms，并发从 1 提到 4 没有稳定收益；该结论只适用于进程内 ASGITransport 与当前数据集，不代表外部部署容量。

## 结论与后续

- 首轮数据可以区分缓存命中和冷解析、有界和无界挖掘、全文与融合重排的成本，足以作为同机回归基线。
- 结构计数均符合预期；挖掘重复扫描被量化为四次调用集读取，后续优化可据此验证是否降到一次。
- 少量 `needs_review` 出现在冷启动和亚毫秒阶段的 p95 比较上，属于尾点噪声；默认报告只标记，不拦截。要开启计时门禁，应先在稳定机器上重新冻结基线。
- 本轮未测真实模型、Qdrant、无界 PrefixSpan 与全仓索引；这些留在显式容量档，不与本轮数字比较。
