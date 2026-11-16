# 部署与运维说明

## 最低部署要求

- Python 环境经包管理器同步；库、仓库、快照三路径缺省均为家目录下绝对路径且相互独立，文件中的相对值按文件目录解析，环境中的相对值按启动目录解析一次并冻结，生产建议全部使用绝对路径。
- 向量默认走本地引擎（`vector_backend=simvec`，进程内，无需外部服务）；选 Qdrant 后端时服务需可达（`qdrant_url`，默认 `http://localhost:6333`，外部实例经 `qdrant_api_key`/`qdrant_https` 鉴权，批量写密集时用 `qdrant_grpc` 复用连接），不可达时检索退化为纯全文并标记降级。
- 向量集合设置分两档：**结构档**（`qdrant_on_disk_*`、`qdrant_shard_number`、`qdrant_replication_factor`、`qdrant_write_consistency_factor`）只在建集合时生效，改动即删集合重建并按真源重灌；**调优档**（`qdrant_hnsw_*`、`qdrant_full_scan_threshold`、`qdrant_memmap_threshold_kb`、`qdrant_indexing_threshold_kb`）改写活动集合、已有点保留。两档都记在 `schema_meta`，漂移自动纠正；本地引擎的建图参数属结构档（引擎在创建时固定）。
- 运维探针：`libref vectors-status` 输出集合画像、可达性、点数、过滤索引（不可达时退出码 1）；`libref vectors-optimize` 就地应用调优档并立即触发本地引擎建图（引擎后台维护周期为 30 s，批量灌库后不必等待）。
- 密钥经环境变量或仓库根环境文件注入，模板见 `.env.example`，真实文件不进版本库。

## 隔离语义

- 单机默认 SQLite 为唯一真源，向量为可重建派生索引；中心部署切换 PostgreSQL 元数据后端（`metadata_backend=postgres`，连接串仅经环境变量注入），全文切关系库原生全文（`fulltext_backend=postgres`）。任一后端切换即全量重建/重新摄取，不迁移旧数据。
- 向量集合名按数据库文件绝对路径哈希派生，内存库保持原名；声明部署身份（`deployment_id`，为空时回落到 `instance_id`）时按部署标识哈希派生，多副本共享同一集合。不同库文件天然隔离；同库多租户靠行级 `tenant_id` 与向量过滤隔离（读路径与向量下推为后续任务，任务与审核反馈已按租户隔离）。
- 模型名或维度变更触发集合删除重建，重灌按真源全量执行；集合画像（后端、距离、索引档与建图参数）一并记录在 `schema_meta`，任一画像项变更同样触发重建，检索期参数与连接参数不影响集合。本地引擎默认 `simvec_index=hnsw`（超过 `simvec_full_scan_threshold` 自动建图），`flat` 为永久精确扫描档。

## 多副本与优雅退出

- 中心模式允许多服务副本共享 PostgreSQL 元数据后端；任务认领与版本锁依赖条件更新与行锁，副本本地缓存（robots/registry/discovery/ast）不共享。
- 每个副本启动时把配置快照指纹写入 `schema_meta`（`config_fingerprint`）并核对，不一致的副本拒绝接流量并记审计；变更配置需滚动重启全部副本。
- 服务退出时执行 drain：停止接新任务、等待在途任务完成（超时上限 `drain_timeout`，默认 30 秒）后释放存储；超时未完成的任务由其余副本经心跳恢复接管。

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
