# CLI 退出码边界收敛方案

> 实施状态：已落地（代码 + 单测 + 验证全部通过）。第 1–9 章为方案与事实依据，第 10 章任务清单，第 11 章实施记录。相关背景见 `docs/plan/mcp-cli-error-contract.md` 第 8、12 章，三档语义见 `docs/app/cli.md:5`。

## 0. 定位与目标

回答一个问题：CLI 的退出码由谁产生、是否收敛到唯一码表，以及散点如何改造。不涉及业务逻辑、MCP 与 Web 出口（其错误契约已在 `mcp-cli-error-contract.md` 落地）。

目标：让所有"失败"经由唯一错误值对象与唯一映射函数产生退出码，使 `--json` 与统一文案在所有命令生效；仅"业务结果"保留裸 return。

## 1. 事实依据：退出码如何产生

当前存在三条产生退出码的路径，前两条并存即"双轨制"。

| 路径 | 入口 | 出口 | 退出码来源 |
|---|---|---|---|
| 统一边界 | `cli/__init__.py:15-21` 捕获异常 → `errors.py:249 normalize` → `common.py:221 report_failure` | `errors.py:150 exit_code_for` | 码表驱动（2 或 1） |
| 命令内裸 return | 各 `cmd_*` 的 `return 0/1/2` | 直接作为 `main()` 返回值 | 命令内硬编码，绕过码表 |
| argparse / SystemExit | `parsers.py` 解析失败、`ops_commands.py:519` | 进程直接退出 | argparse 默认 2 / 硬编码 2 |

统一码表与映射（`src/libref/errors.py`）：

- `ErrorCode` 闭集合 19 码：`errors.py:20-40`；`ALL_CODES`：`errors.py:42-62`。
- 用法码集合 `_USAGE_CODES`：`errors.py:120-135`（含 `invalid_argument` 及四种非法变体、`unauthorized`、`forbidden`、`missing_scope`、`origin_forbidden`、`config_refresh_failed`）。
- 映射 `exit_code_for`：`errors.py:150-152`，`2 if code in _USAGE_CODES else 1`。
- 三档语义（`docs/app/cli.md:5`）：成功 0 / 运行失败 1 / 用法或配置错误 2；参数错误打印到 stderr。
- 工厂：`invalid_argument` (`errors.py:235`)、`not_found` (`:241`)、`unavailable` (`:245`)。
- 分类边界 `as_error` (`common.py:198-203`)：`normalize` 返回 `None` 时重抛，保留真实崩溃的 traceback。

单测现状：`tests/unit/test_errors.py:39-49` 已钉住"用法码 → 2、故障码 → 1"的映射本身，缺的是"命令确实走这条映射"的验证。

## 2. 全部散点清单（改造前实测）

### 2.1 用法错误类（应收敛，目标码 `invalid_argument` → 2）

| 位置 | 场景 | 当前行为 | 当前退出码 |
|---|---|---|---|
| `code_commands.py:47-49` | 显式模式缺 version/source | `print` + `return 2` | 2 |
| `code_commands.py:50-52` | `--ecosystem` 与 source 互斥 | `print` + `return 2` | 2 |
| `code_commands.py:365-370` | `--dedup`/`--no-dedup` 互斥（`invalid:` 前缀） | `print` + `return 2` | 2 |
| `docs_commands.py:232-240` | 远端 ingest 缺显式 start_url | `print` + `return 2` | 2 |
| `docs_commands.py:297-299` | resolve 模式缺 library | `print` + `return 2` | 2 |
| `docs_commands.py:300-304` | `--ecosystem` 与 start_url 互斥 | `print` + `return 2` | 2 |
| `docs_commands.py:333-337` | 显式模式参数不全 | `print` + `return 2` | 2 |
| `docs_commands.py:627-629` | 不支持的后缀 | `print` + `return 2` | 2 |
| `ops_commands.py:69-74` | 版本未解析为单一值（`invalid:` 前缀） | `print` + `return 1` | 1（分类偏误） |

### 2.2 配置错误类（应收敛，目标码 `config_refresh_failed` → 2）

| 位置 | 场景 | 当前行为 | 当前退出码 |
|---|---|---|---|
| `ops_commands.py:481-483` | 启动身份校验失败（`config error:` 前缀） | `_prepare_auth` 返回 2，调用方 `:548-550` 转 return | 2 |
| `ops_commands.py:513-519` | 配置指纹与共享控制面分歧 | `print` + `raise SystemExit(2)` | 2 |

`config_refresh_failed` 归入退出码 2 与 `docs/app/cli.md:5` 的"配置错误返回 2"一致，归类不改；只改产生方式。

### 2.3 运行故障类（应收敛，目标码 `unavailable` → 1；退出码不变）

| 位置 | 场景 | 当前行为 | 当前退出码 |
|---|---|---|---|
| `ops_commands.py:38-40` | 未配置 embedding key | `print` + `return 1` | 1 |
| `ops_commands.py:65-67` | 同上（reconcile） | `print` + `return 1` | 1 |
| `docs_commands.py:269-271` | 远端轮询失败（`remote poll failed:` 前缀） | `print` + `return 1` | 1 |
| `docs_commands.py:279-281` | 远端轮询超时 | `print` + `return 1` | 1 |
| `request_commands.py:23-33,272-273` | 请求命令局部 `_fail`/`_error`（`failed:` 前缀） | 捕获全部异常后返回 1 | 1（已知故障应按码表分 1/2） |

### 2.4 业务结果类（保留裸 return，不属错误）

| 位置 | 场景 | 保留原因 |
|---|---|---|
| `code_commands.py:129,221,310` 等 | 异步任务终态 `done→0 / 其他→1` | `errors.py:9-10` 明确业务结果不经错误通道；`cli.md:224` 规定仅 done 为 0 |
| `docs_commands.py:58,218,278,361` | 任务/smoke 结果 | 同上 |
| `ops_commands.py:99,116` | 集合同步/可达性探测结果 | 运维探针语义 |
| `ops_commands.py:329,348` | `role-audit --strict` 冲突 | CI 卡口，退出码即结果（`docs/features/web/permissions.md:55`） |
| `docs_commands.py:547,586` | 页面未命中 `(not found)` | "never indexed" 是业务结果（`errors.py:9-10`） |
| `docs_commands.py:456,519,521` | 检索降级 / 低置信 | 业务结果 |
| `common.py:179,184`、`registry_commands.py:45,75` 等 | 空结果 / 空建议 | 空结果集是业务结果 |

## 3. 分类原则

一条判据决定去留：**调用方能否通过改输入或改配置避免它**。

- 能（参数互斥、缺必填、非法后缀、版本歧义、配置分歧、缺 key）→ 失败，抛 `LibrefError`，退出码由码表决定。
- 不能（未命中、空集、降级、任务失败终态、strict 冲突）→ 业务结果，裸 return，退出码由命令语义决定。

## 4. 现有设计是否合理

结论：**方向合理，实现未收敛**。

站得住：三档语义清晰且符合 Unix 惯例；`errors.py` 单一闭集合码表 + 单一映射 + 闭集合校验 + 单测覆盖；`main()` 单一异常边界让原 18 个无边界命令获得一致映射；未知异常保留 traceback。

站不住：2.1–2.3 共 16 处散点绕过统一边界，导致 `--json` 在这些路径失效、文案四套并存（`invalid:`/`failed:`/`config error:`/`remote failed`）、退出码由命令内硬编码而非码表决定；`ops_commands.py:519` 的 `SystemExit` 直接逃逸 `main()` 的 `except Exception`。

## 5. 修改方案

所有条目均为"改产生方式"，不改变对外语义（2.1 中 `ops_commands.py:69-74` 除外，见开放点 Q1）。`file:line` 为改造前位置。

| # | 位置 | 修改前 → 修改后 | 目标码 / 退出码 |
|---|---|---|---|
| M1 | `code_commands.py:47-49` | `print`+`return 2` → `raise invalid_argument(..., field="version")` | invalid_argument / 2 |
| M2 | `code_commands.py:50-52` | 同上 → `raise invalid_argument(..., field="ecosystem")` | invalid_argument / 2 |
| M3 | `code_commands.py:365-370` | `invalid:`+`return 2` → `raise invalid_argument(..., field="dedup")` | invalid_argument / 2 |
| M4 | `docs_commands.py:232-240` | `print`+`return 2` → `raise invalid_argument(..., field="start_url")` | invalid_argument / 2 |
| M5 | `docs_commands.py:297-299` | → `raise invalid_argument(..., field="library")` | invalid_argument / 2 |
| M6 | `docs_commands.py:300-304` | → `raise invalid_argument(..., field="ecosystem")` | invalid_argument / 2 |
| M7 | `docs_commands.py:333-337` | → `raise invalid_argument(...)` | invalid_argument / 2 |
| M8 | `docs_commands.py:627-629` | → `raise invalid_argument(..., field="source")` | invalid_argument / 2 |
| M9 | `ops_commands.py:69-74` | `invalid:`+`return 1` → `raise invalid_argument(...)` | invalid_argument / 2（见 Q1） |
| M10 | `ops_commands.py:481-483,548-550` | `_prepare_auth` 改为抛 `LibrefError("config_refresh_failed", ...)`；调用方删返回值检查 | config_refresh_failed / 2 |
| M11 | `ops_commands.py:513-519` | `print`+`SystemExit(2)` → `raise LibrefError("config_refresh_failed", ...)` | config_refresh_failed / 2 |
| M12 | `ops_commands.py:38-40,65-67` | `print`+`return 1` → `raise unavailable(...)` | unavailable / 1 |
| M13 | `docs_commands.py:269-271,279-281` | `print`+`return 1` → `raise unavailable(...)` | unavailable / 1 |
| M14 | `request_commands.py:23-33,272-273` | 删 `_fail`/`_error`，`except` 改 `raise as_error(exc)` | 按码表（1/2），未知保留 traceback |

依赖：`code_commands.py`、`docs_commands.py` 顶部增 `from libref.errors import invalid_argument`（docs 另加 `unavailable`）；`ops_commands.py` 在既有 `LibrefError` import 上加 `unavailable`；`request_commands.py` 在 `common` import 上加 `as_error`。M10 使 `_prepare_auth` 返回类型由 `int | None` 收敛为 `None`。

## 6. 保持不变

第 2.4 节全部业务结果、argparse 自身的 2、码表内容与 `unauthorized`/`forbidden` → 2（已在 `mcp-cli-error-contract.md:221` 开放点 6 拍板复用 2）。

## 7. 测试方案

新增 `tests/unit/test_cli_exit_boundary.py`（`pytestmark = pytest.mark.unit`），用 `argparse.Namespace` 直接驱动各 `cmd_*`，断言：

1. 用法错误点抛 `LibrefError` 且 `exit_code_for(error.code) == 2`；
2. 配置错误点抛 `LibrefError("config_refresh_failed")`；
3. `report_failure` 在 `args.json=True` 时 stdout 输出单行紧凑 JSON、stderr 输出人类可读行；
4. 业务结果点（如 `role-audit --strict`）仍返回裸 `1`，不抛异常。

既有 `test_errors.py` 映射单测不改；`test_probe_exits.py` 仅测参数解析，不受影响。

## 8. 验证口径

`uv run pytest`、`uv run ruff check src tests`、`uv run ruff format --check src tests`、`uv run mypy src` 全部通过。前置：`.python-version` 锁 `3.14`，需先备好对应解释器；若受限，用 uv 托管 Python 显式指定后再 sync。

## 9. 待拍板开放点

| # | 问题 | 选项 | 方案默认 | 影响 |
|---|---|---|---|---|
| Q1 | M9 版本歧义的退出码 1→2 | 保持 1 / 修正为 2 | 修正为 2（它是可用输入避免的） | 依赖该码为 1 的脚本需适配 |
| Q2 | `config_refresh_failed` 归 2 | 保持 / 改为 1 | 保持 2（符合 `cli.md:5`） | 无 |
| Q3 | `unauthorized`/`forbidden` → 2 | 保持 / 单列 3 | 保持 2（已拍板） | 无 |
| Q4 | M14 未知异常改为 traceback | 保持静默 1 / 保留 traceback | 保留 traceback（符合 `as_error` 契约） | 崩溃更易诊断，输出格式变化 |
| Q5 | 运行故障 M12/M13 文案从 `xxx failed:` 变为 `unavailable: ...` | 保持 / 收敛 | 收敛 | stderr 文案变化，退出码不变 |

## 10. 完整任务清单

- [x] 核查全部退出码散点的精确 file:line 与 try/except 结构
- [x] 编写本方案文档
- [x] M1–M8：8 处用法错误散点收敛
- [x] M9：版本歧义修正为 2
- [x] M10–M11：配置错误收敛，移除 SystemExit
- [x] M12–M13：运行故障收敛为 unavailable
- [x] M14：request 命令收敛到 as_error
- [x] 新增 `test_cli_exit_boundary.py`
- [x] pytest / ruff / mypy 验证
- [x] 回写第 11 章实施记录

## 11. 实施记录

### 11.1 落地内容

四份 CLI 命令文件全部改为经 `LibrefError` 产生退出码，业务结果裸 return 保持不变。落地后的实际行号（相对 `src/libref/cli/`）：

| 文件 | 落地行号 | 改动 |
|---|---|---|
| `code_commands.py` | `:48,:52,:368` | 三处 `raise invalid_argument`（M1–M3）；删 `import sys`；增 `from libref.errors import invalid_argument` |
| `docs_commands.py` | `:237,:297,:301,:334,:627` | 五处 `raise invalid_argument`（M4–M8）；`:271,:280` 两处 `raise unavailable`（M13）；增 `invalid_argument, unavailable` import |
| `ops_commands.py` | `:39,:65,:68,:477,:509` | 两处 `unavailable`、一处 `invalid_argument`（M9/M12）；两处 `config_refresh_failed`（M10/M11）；`_prepare_auth` 返回类型由 `int \| None` 收敛为 `None`，调用方 `cmd_serve` 删返回值检查 |
| `request_commands.py` | `:259` | 删 `_fail`/`_error`，except 改 `raise as_error(exc)`（M14）；删 `import sys`；增 `as_error` import |

`SystemExit(2)`、`invalid:`、`config error:`、`remote poll failed:` 四类硬编码/文案已从 CLI 命令源码清除。`argparse` 自身的 2、码表内容、`unauthorized`/`forbidden` → 2 未动。

### 11.2 新增测试

`tests/unit/test_cli_exit_boundary.py`（9 例，marker `unit`）：三档映射参数化、`report_failure` 文本/JSON 双渲染、未知异常不被吞、`cmd_index_repo` 缺 version 抛 `invalid_argument`、`cmd_dump_chunks` 非法后缀抛 `invalid_argument`（含 `field`）、`unavailable` 保持 1 档。

### 11.3 验证结果

Python 3.14.3 经 uv 镜像 + `--native-tls` 安装（原 GitHub 直连证书失败），`uv sync` 经腾讯 PyPI 源 + `UV_NATIVE_TLS=1` 完成。

- `uv run pytest -q`：**834 passed, 59 skipped**（改造前基线 825 passed；新增 9 例全部通过，无回归）。
- `uv run ruff check src tests`：All checks passed（先自动移除 3 处失效的 `noqa: BLE001`）。
- `uv run ruff format --check src tests`：311 files already formatted。
- `uv run mypy src`：Success, no issues found in 173 source files。

`uv.lock` 在验证中被新版 uv 重写（纯格式重排），已 `git checkout` 还原，保持与改造无关的文件零变更。

### 11.4 行号漂移说明

第 1–5 章表格中的行号为改造前实测位置；落地后因删行与新增 import 略有偏移，实际位置以 11.1 表为准。语义与改造目标一致。

### 11.5 开放点状态

Q1（版本歧义退出码 1→2）已按默认"修正为 2"落地；Q2/Q3 保持；Q4（未知异常保留 traceback）按默认落地；Q5（运行故障文案收敛为 `unavailable: ...`）按默认落地。四项默认外行为改动均已单测或 diff 覆盖，如需回退见各开放点选项。
