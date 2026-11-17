# 端点到前端需求的映射

> 按 `src/libref/web/app.py` 路由逐条列出前端调用方式、所需权限 scope 与错误处理。
> 信封约定：列表类返回 items 加 degraded 加 count；单体类直接返回对象。

## 只读检索与阅读（scope: read）

| 端点 | 前端用法 | 要点 |
|---|---|---|
| `GET /catalog` | 文库目录页 | 无库名返回库列表，有库名返回版本列表；生态可选 |
| `GET /search` | 搜索页 | 必填 query；档位三选一；limit 上限 100；留意 answerable/answerability/reasons/suggestions 附加字段 |
| `GET /answer` | 问答页 | limit 上限 50；chat 未启用后端报 400，前端转友好提示 |
| `GET /read` | 文档阅读器 | mode 三选一；window 模式需 sort_order 加 radius（上限 20）；404 表示页面未收录 |
| `GET /library-search` | 入库向导辅助 | registry 联想；失败为 502，前端标注来源异常 |
| `GET /resolve` | 入库向导第一步 | target 取 docs 或 repo；no_docs/no_repo 为 404，其余上游失败为 502 |
| `GET /web-search`、`GET /github-search` | 入库向导辅助 | limit 范围 1 到 100；空查询前端拦截 |
| `GET /repo-deps` | 版本详情附属 | library 与 version 必填；租户不可见按 404 处理 |
| `GET /link-stats` | 阅读器附属 | 有 url 返回单页统计，否则返回版本汇总加热门引用；limit 上限 500 |
| `GET /mining-health` | 模式页健康区 | 库版本生态全可选，无参返回全局健康 |

## 源码洞察（scope: read）

| 端点 | 视图参数 | 前端注意 |
|---|---|---|
| `GET /api-insight` | view 取 usage/related/args/common | related 与 args 必填 api；common 填 api 或 apis（逗号分隔）；limit 上限 500 |
| `GET /pattern-insight` | view 取 patterns/evidence/tree/clusters/health/labels/snippets | pattern 与 containing 为逗号分隔序列；prefix 为单前缀；dedup 与 per_file_limit 只在部分视图有效 |
| `GET /symbol-insight` | view 取 detail/file/children/signature | 支持 offset 分页；apis 多值逗号分隔 |
| `GET /call-insight` | view 取 callee/caller/file | resolve_kinds 逗号分隔多选；min_args/max_args 为非负整数；offset 分页 |
| `GET /import-insight` | view 取 importers/imports | offset 分页 |

## 写操作：入库与任务

| 端点 | 权限 | 前端注意 |
|---|---|---|
| `POST /doc-probe` | read | scope 为对象；fetch_sample_limit 上限 100；max_pages 上限由后端校验；地址不合规 400 |
| `POST /repo-probe` | read | only_files 与 exclude_patterns 非空即合法由前端先验；tree_depth 上限 10 |
| `POST /scrape` | ingest:write | 配额拒绝为 429（含 reset_at）；成功返回任务 id 即跳转任务中心 |
| `GET /jobs`、`GET /jobs/{id}` | read | 详情 404 表示任务不存在或跨租户不可见 |
| `POST /jobs` | jobs:write | kind 加 params 对象；429 同上 |
| `POST /jobs/{id}/cancel` | jobs:write | 幂等语义，重复取消仍成功 |

## 写操作：审核与反馈

| 端点 | 权限 | 前端注意 |
|---|---|---|
| `GET /review` | review:read | action 固定 list；limit 上限 500 |
| `POST /review` | review:write | approve/conflict/reopen 需 entity_id；非法流转 409 需展示原文 |
| `POST /feedback` | feedback:write | entity_type 默认为 call_instance；正文必填 |
| `GET /feedback` | feedback:read | limit 上限 200；status 可选过滤 |
| `POST /feedback/{id}/resolve` | feedback:resolve | 不存在为 404；成功后列表即时置灰 |

## 系统管理

| 端点 | 权限 | 前端注意 |
|---|---|---|
| `GET /health` | 无需鉴权 | 返回 ok 加指纹，用于页眉状态点 |
| `GET /readyz` | 无需鉴权 | checks 逐项展示，sqlite/vector/embed/chat 四项；degraded 为真即标黄 |
| `GET /metrics` | read | 进程计数快照，管理页轮询展示 |
| `GET /admin/keys` | admin:keys | 列表展示前缀、权限域与吊销状态 |
| `POST /admin/keys` | admin:keys | 返回原文 key，仅展示一次并提供复制；可选有效期（expires_at） |
| `DELETE /admin/keys/{id}` | admin:keys | 不存在为 404；需二次确认 |
| `POST /admin/config/refresh` | admin:config | 返回新旧指纹，对比后提示是否变化 |

## 通用错误与鉴权处理

- 401 未授权：dev 模式不会出现；enforced 模式跳转登录（输入 API Key）并重试原请求。
- 403 禁止：缺 scope 时隐藏对应入口，已在页面内则展示无权限说明而非空白。
- 429 配额超限：展示资源名与 reset_at，倒计时后允许重试；静态页面刷新不计配额，
  只有 API 与 MCP 计数（见 frontend-hosting 鉴权边界）。
- 400 参数错误：展示后端 detail 原文；前端对必填与数值范围做先验，减少往返。
- 502 上游失败：registry 与外部搜索专属，标注数据来源异常并保留已缓存内容。
- 每个请求携带的 x-request-id 响应头在报错时展示，便于与审计日志对账。
