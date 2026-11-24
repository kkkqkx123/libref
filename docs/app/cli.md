# 服务端 CLI

`libref` 二进制只用于服务端启动与本机运维，命令实现位于 `src/libref/cli/`。面向调用方的检索、探查、索引申请等命令已迁至独立客户端 `librefctl`，见 `cli-client.md`。服务端 CLI 不读取 `LIBREF_BASE_URL`，也没有 `--remote`：所有命令直接操作本机配置、数据库与任务队列，只能在服务端主机使用。

全局选项 `--config`、`--log-level`、`--log-file`、`--log-format` 覆盖配置文件同名项；`--json` 让失败以 JSON 打印。成功返回 0，运行失败返回 1，用法或配置错误返回 2。

## 服务启动

* `serve`：单进程提供 Web 路由与挂载在 `mcp_path` 的 `/mcp`，监听取 `web_host/web_port`，`--host/--port` 可覆盖。`--enabled-tools/--disabled-tools` 裁剪 MCP 工具。启动校验：dev 模式非回环拒绝，enforced 模式无引导凭据且无主体时拒绝，失败返回 2。
* `serve-mcp` 与 `serve --stdio`：同一条标准输入输出组装路径，供本机 MCP 客户端使用；二者共用运行时上下文、启动校验、配置指纹校验和退出排空。流式 HTTP 只由不带 `--stdio` 的 `serve` 提供。

## 本机运维

* `init-db`：建库并应用迁移，打印库路径与已应用迁移数。
* `smoke-local`：起临时站点验证发现到存储全链，不依赖外部网络，`stored` 大于零即成功。
* `reembed`：`library/version` 必填，`--ecosystem` 可选。按真源重嵌；嵌入未配置返回 1。
* `reconcile`：参数同 `reembed`。按集合差修复单版本向量，输出真源数、向量数、缺失、孤儿、写入与重建数。
* `fts-rebuild/fts-check`：无参数，输出文档数、采样数、缺失数与同步标记，不一致时返回 1。
* `vectors-status/vectors-optimize`：查看向量集合画像与可达性，或立即发布待处理索引维护。
* `review`：`list [--limit 50]` 列待定项；`approve/conflict/reopen entity_id` 流转单实体。非法流转返回 1。此为服务端本机口径；远端调用方用 `librefctl review`。
* `job-show/job-cancel`：查看或取消本机任务。`job-show` 的 `job_id` 可选，`--list` 列最近任务，`--limit` 默认 20，`--full` 打印完整结果；未知编号返回 1。
* `config-show`：打印配置文件路径、配置指纹与脱敏后的有效配置。
* `purge`：`library/version` 必填，`--ecosystem` 可选。整版本删除并记录，输出各计数。
* `deletions`：`--limit` 默认 50，`--full` 打印详情，`--prune` 按保留期清理，`--ttl-days` 缺省取配置。
* `role-audit`：巡检角色互斥，逐行列出冲突主体并给出可执行的 `--keep` 选项；`--json` 输出结构化清单，`--strict` 存在冲突时返回 1。
* `role-split`：`--principal` 与 `--keep` 必填，`--name` 覆盖新主体命名，`--dry-run` 只打印计划，`--json` 输出结构化结果。执行时把非保留角色装箱到最少量新主体，每个新主体签发一把 key 并只回显一次明文；主体不存在、无冲突或 `--keep` 不在其角色中返回 2。

## 本机数据与挖掘

这些命令收在 `local` 子命令组，只在服务端主机使用，直接打开本机数据库与文件系统；需要远程调用同类能力时使用 `librefctl`，不要在服务端 CLI 上叠加远端路径。文档探查统一走 `librefctl doc-probe`，服务端不再提供入口。

* `local index-repo library version source`：`source` 只接受本机路径；远端仓库必须由调用方经 `librefctl request submit` 申请并审批。按服务层默认参数直接入库，支持 `--subdir`、`--only-file`、`--exclude-pattern`、`--max-files`、`--source-role`、`--target-prefix`、`--target-name`、`--update`、`--fresh`。
* `local probe-repo source`：只读探查本机路径或远端仓库的目录形状，不解析文件、不落库；过滤选项与 `index-repo` 同名。
* `local mine library version`：直接调用挖掘服务；`--resolve-kind`、`--source-role` 可重复，`--cluster-threshold`、`--cluster-min-size`、`--macro-policy` 与服务层同名。
* `local induce-labels library version`：直接调用标签归纳，使用本机配置的 chat 端点。
* `local doc-features refresh --library --version`：重建一个版本的派生特征，`--scope` 指定单一视角或 `all`。

## 主体与密钥

本机命令以平台级系统调用者执行，复用服务层校验并写入 `cli.*` 审计；明文 key 只在签发命令的 stdout 出现一次。

* `principal list [--tenant TENANT]`：列出主体、角色、scope 与冲突标记。
* `key list [--tenant TENANT]`：列出 key 元数据，不含明文。
* `key issue --name NAME [--kind KIND] [--tenant TENANT] [--role ROLE]... [--scope SCOPE]... [--expires-at ISO]`：创建主体并签发 key。
* `key attach PRINCIPAL_ID [--scope SCOPE]... [--expires-at ISO]`：给既有主体追加 key。
* `key revoke KEY_ID`：撤销单把 key。
* `key revoke-all PRINCIPAL_ID`：撤销主体全部有效 key。

`--json` 输出结构化结果，签发结果中含一次性明文 `key`。角色变更仍由 `role-split` 或 Web 管理路由负责。

## 配置

服务端配置定义见同目录 `config.md`。客户端地址与凭据不属于服务端配置：凭据只经环境变量注入，`config-show` 打印脱敏快照。
