# 数据库查询审计离线核对

本目录对应 `docs/features/db/` 现状与 `docs/archive/db-query-decisions.md` 决策的落地核对，全部离线运行，
不需要真实网络、向量后端与语言模型。

## 运行

```bash
uv run python tests/e2e/db-query/debug_queries.py > tests/e2e/db-query/outputs/audit-checks.txt 2>&1
```

脚本自带断言式核对项，任一项失败即非零退出。
输出报告落到 `outputs/` 目录，仅供人工阅读，不参与断言。

## 核对范围

查询计划：读节过滤、待审列表、证据剪枝、作用域全文（实测语句抓取）、纯地址读页。
行为：换序复用不撞键、分路复用隔离、未变更新凭证、精确分制方向、
作用域检索不出版本、作用域匹配标识不出版本、待审排序与截断下推、
证据两阶段剪枝、作用域列回退、源码幽灵页清理、全文自检通过。
