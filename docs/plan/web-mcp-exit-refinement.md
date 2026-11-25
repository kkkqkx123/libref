# Web 与 MCP 出口 refinement 设计

## 背景

对三个出口（`cli/`、`mcp_server/`、`web/`）的功能盘点与设计评审确认：分层纪律（出口只经 `services/`）、职责差异化（CLI 运维、MCP 面向模型、Web 全功能）、横切关注点（scope 鉴权、租户、审计、错误归一化）整体成立。评审同时暴露四类可维护性与一致性问题，本文给出问题清单与分阶段修复方案。

## 问题清单

1. **Web 路由连接关闭样板重复。** 每个使用 `req_bundle` 的路由都重复同一段 `finally` 中 `if should_close: try: rc.close() except: pass` 的收尾，`request_context.py` 的 `quota_gate` 也含一份，全仓超过三十处。样板分散意味着任何收尾语义变化（如增加指标、延迟关闭）都要改三十处。
2. **mining insight 入参校验双份维护。** Web（`api.py` 的 query 模型 + `routes/mining.py` 的 `Literal` 视图参数）与 MCP（`tools/mining.py` 中逐个函数的 view/limit 校验）各自实现同一组校验：合法 view 集合、`view` 与 `api` 的依赖关系、行数上限。两处已能观察到措辞与默认值漂移的风险，且与 `services/code/mining_read` 的真实分支之间无单一事实来源。
3. **MCP 外呼工具缺配额门。** Web 侧对触发外部调用的提交（`/scrape`、`/jobs` 等）走 `quota_gate`；MCP 的 `wrap` 只做 scope、租户与审计。`answer`、`web_search`、`github_search`、`resolve` 四个工具会触发 LLM 或注册表网络请求，在多租户场景下不受配额约束，与 Web 侧不对称。
4. **`resolve` 工具出网属性不醒目。** MCP discovery 组内其余工具均为库内查询，唯 `resolve` 实时抓取注册表；当前工具描述仅一句 "refreshes the cache"，对调用方（模型）的风险提示不足。

## 决策

- 连接收尾收敛为 `request_context.py` 中的一个关闭助手：`req_bundle` 返回的连接由单一函数负责条件关闭，所有路由与 `quota_gate` 改为在 `finally` 中调用该助手。行为不变，只消除重复。
- mining insight 的合法视图集合、视图与 `api` 参数的依赖规则、行数上限，统一定义在 `services/code/mining_read` 层并导出；Web 路由与 MCP 工具改为引用同一常量与同一校验函数，服务层仍是唯一事实来源。
- MCP 配额门复用 `services/infra/quota` 的现有机制：在 `McpContext.wrap` 内对外呼工具按资源名执行 `enforce`，被拒时返回与 Web 相同的配额错误形态。资源名与 Web 侧已有命名保持一致风格；配额未启用时直通，默认部署不受影响。
- `resolve` 工具描述明确标注其实时出网、结果即注册表实况，与其余库内查询工具区分。
- 不改任何对外契约字段、不新增兼容别名、不动 CLI 启动路径（stdio 合并已有独立方案文档覆盖）。

## 任务顺序

1. 新增连接关闭助手并替换全部重复收尾（`web/request_context.py` 与九个路由文件）。
2. 在 `services/code/mining_read` 定义并导出视图校验；`web/routes/mining.py`、`web/api.py` 与 `mcp_server/tools/mining.py` 改为共享校验。
3. `McpContext.wrap` 接入配额门，覆盖 `answer`、`web_search`、`github_search`、`resolve`；补最小单测。
4. 更新 `resolve` 工具描述；同步 `docs/architecture/mcp-tools.md` 中受影响的表述。
5. 收尾验证：`ruff`、`mypy`、`pytest` 全量一次通过。

## 验证口径

- 关闭助手替换后，路由行为与现有集成测试结果一致，路由文件中不再出现裸的 `rc.close()` 样板。
- mining 各端点的非法 view 与超限 limit 在 Web 与 MCP 两侧返回一致的错误；新增共享校验的单测覆盖两侧。
- 配额启用且超限时，四个外呼 MCP 工具返回配额拒绝；配额关闭时行为不变。
- 文档与代码表述一致，lint、类型、测试全绿。
