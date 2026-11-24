# CLI 客户端拆分设计

## 背景

当前 `libref` 二进制同时承担两类职责：一是启动本机服务与维护数据库、向量、角色等本机资源；二是面向调用方提供检索、洞察、探查和索引申请。后者已有部分命令带 `--remote` 分支，但覆盖不完整：`answer`、各 `*-insight`、`discover`、两类 `probe` 等在本地直接开库，HTTP 侧却没有对应客户端路径。同一个能力因此存在两份实现，参数与错误语义已经漂移。

为 skill + CLI 执行准备独立客户端，目标是把「skill 调用方」与「服务端本机运维」彻底分开，而不是继续在旧 CLI 上叠加远端分支。

## 目标形态

拆成两个发行单元、两个二进制：

- `librefctl`：独立安装的 HTTP 客户端，供 skill 与脚本调用。只依赖 `httpx` 与标准库，不读服务端配置、不开 SQLite、不装配向量库、不调用 `services/`。所有请求经服务端 Web API，权限由服务端 scope 决定。
- `libref`：保留在 `libref` 发行包中，仅提供 `serve`、`serve-mcp` 与必须触达本机资源的运维命令（建库、迁移、FTS/向量维护、清理、角色巡检与拆分、配置查看、本机任务与审核等）。不再提供 `--remote`，也不再提供面向 skill 的检索与申请命令。

`librefctl` 作为 uv workspace 成员单独发行；服务端发行包不依赖它，只在开发组中安装以便联调。

## 命令边界

`librefctl` 覆盖服务端已暴露的读能力与受控写能力：

- 检索问答：`search`、`answer`、`read`、`catalog`、`resolve`；
- 发现：`discover`、`search-web`、`search-github`；
- 洞察：`api-insight`、`pattern-insight`、`doc-insight`、`symbol-insight`、`call-insight`、`import-insight`、`link-stats`、`repo-deps`、`mining-health`；
- 探查：`doc-probe`、`repo-probe`，其中仓库探查只接受远端地址，不接受本机路径；
- 流程：`request submit/list/show/approve/reject/cancel/wait`、`job list/show/cancel/wait`、`review list/approve/conflict/reopen`、`feedback submit/list/resolve`、`health`、`me`；
- 允许的写入：`ingest-docs` 经 `/scrape` 提交；挖掘与标签生成经 `/jobs` 提交；仓库入库必须经 `/index-requests` 审批。

不进入客户端的能力：本机路径索引与探查、直接挖掘、向量重建、迁移、主体与密钥管理、删除与保留策略。这些操作留在服务端本机 CLI，避免把服务端文件系统和运维权限带到 skill 运行环境。

## 传输与输出契约

客户端只有一个传输层，负责 URL 拼接、Bearer 注入、超时与错误信封转换。服务端错误体中的 `error/detail/request_id` 以及 FastAPI 422 校验体都映射到同一个客户端错误值；网络异常映射为 `unavailable`。

成功与失败都输出单个 JSON 对象：成功为 `ok/data/meta`，失败为 `ok/error`。降级理由、空结果与提示放进 `data` 或 `meta`，不再混写 stderr。退出码沿用三档：用法与配置错误 2，运行失败 1，成功（含业务空结果）0。客户端不复制服务端响应模型：响应体原样放在 `data` 中，字段真相源仍是 Web API 契约。

配置只取三个来源：命令行参数、`LIBREF_BASE_URL`、`LIBREF_CLIENT_API_KEY`，另有可选 `LIBREF_CLIENT_TIMEOUT`。客户端不扫描 `libref.toml`，不从服务端配置推导地址。

## 实施顺序

1. 建立 `librefctl` 包与声明式命令表，先接通只读命令并用 MockTransport 验证参数映射与错误映射。
2. 接通索引申请、任务、审核与反馈流程，等待命令只在客户端轮询服务端状态，不引入本地作业状态。
3. 从 `libref` 的解析器中移除客户端命令与 `--remote`，删除对应的本地命令模块；保留的运维命令去掉远端分支。
4. 同步更新错误提示中的命令名、CLI/MCP/Web 文档、部署说明和测试。最终状态不保留兼容别名或旧双路径。

## 验证口径

- 客户端解析测试覆盖命令表、重复参数、布尔三态和 JSON 参数；
- 传输测试用 MockTransport 覆盖成功、服务端错误信封、422 校验错误和网络失败；
- 等待命令用伪响应验证终态判定与超时；
- 服务端 CLI 测试只保留本机运维命令的解析与错误边界；
- 检索与挖掘相关命令的差分测试以 Web API 响应为基准，客户端只做透传与封装。
