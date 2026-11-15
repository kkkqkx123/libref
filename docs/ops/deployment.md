# 部署与运维说明

## 最低部署要求

- Python 环境经包管理器同步；库、仓库、快照三路径缺省均为家目录下绝对路径且相互独立，文件中的相对值按文件目录解析，环境中的相对值按启动目录解析一次并冻结，生产建议全部使用绝对路径。
- 向量服务需可达，默认 `http://localhost:6333`；不可达时检索退化为纯全文并标记降级。
- 密钥经环境变量或仓库根环境文件注入，模板见 `.env.example`，真实文件不进版本库。

## 隔离语义

- SQLite 为唯一真源，向量为可重建派生索引。
- 向量集合名按数据库文件绝对路径哈希派生，内存库保持原名；不同库文件天然隔离；同库多租户靠行级 `tenant_id` 与向量过滤隔离（读路径与向量下推为后续任务，任务与审核反馈已按租户隔离）。
- 模型名或维度变更触发集合删除重建，重灌按真源全量执行。

## 暴露面规则

- 接口默认只绑本地回环。`dev` 模式跳过 key 校验，非回环 dev 直接拒绝启动；`enforced` 模式每请求需 `Authorization: Bearer`，首次启动需 `LIBREF_BOOTSTRAP_ADMIN_KEY` 或已有主体，否则拒绝启动。
- 密钥只走环境变量，不进配置文件；请求侧用 Authorization Bearer 传递。模板见 `deploy/env.dev.example` 与 `deploy/env.prod.example`，真实文件不进版本库。

## 常用操作

- 初始化：`libref init-db`。
- 抓取与索引：`libref ingest`、`libref index-repo`，异步形态经任务队列执行。
- 重灌向量：`libref reembed`，对账语义为版本内向量点数等于文档表计数（全文小块除外）。
- 审核：`libref review list|approve|conflict|reopen`，非法转移拒绝。
- 删除与清理：`libref purge` 整版删除并记日志，`libref deletions --prune` 按保留期清理。
- 服务：`libref serve`（组合 Web 与 `/mcp`，`--stdio` 切标准输入输出）；旧 `serve-mcp [--http]`（用 `[mcp]` 监听）与 `serve-web` 保留；配置脱敏查看用 `libref config-show`。
- 任务：`POST /scrape` 与 `POST /jobs` 只提交，后台 worker 原子认领执行；`GET /jobs` 按租户过滤。

## 配额与保留期

- 快照目录与配额、仓库配额、删除日志保留期见留存节配置；超配额写入拒绝并计数。
