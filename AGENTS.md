# AGENTS.md — libref 开发环境与协作约定

## 环境管理（uv，唯一入口）

本项目所有 Python 环境操作**只经 uv**，不使用系统 pip / 全局安装：

```bash
uv sync                    # 按 uv.lock 创建/同步 .venv（含 dev 组）
uv sync --group dev        # 显式含开发依赖（默认 dependency-groups.dev 已随 sync 安装）
uv add <pkg>               # 新增运行依赖（同时更新 pyproject 与 uv.lock）
uv add --optional browser <pkg>   # 可选 extra（如 playwright）
uv run <cmd>               # 在 .venv 中执行任意命令（pytest、python 等）
uv run pytest              # 运行测试
uv run python -c "..."     # 一次性脚本
```

- 虚拟环境固定在项目根 `.venv/`（`uv sync` 自动创建），**不要**手动 `source .venv/bin/activate` 之外的方式激活，也**不要**直接调用 `.venv/bin/python` —— 一律走 `uv run`。
- 依赖变更只允许通过 `uv add` / `uv remove` / 直接编辑 `pyproject.toml` 后 `uv sync`，禁止 `pip install`（会产生与 uv.lock 不一致的环境）。
- Python 版本由 `.python-version`（3.14）钉住；升级版本须同步核对 tree-sitter grammar wheel、sqlite-vec 等二进制依赖兼容性（见 docs/architecture/tech-stack.md §6）。

## 项目布局

- src 布局：源码在 `src/libref/`，测试在 `tests/`，结构与模块依赖规则见 `docs/architecture/module-layout.md`。
- 技术选型与依赖分阶段清单：`docs/architecture/tech-stack.md`。
- 对标项目蓝本分析：`docs/ref/`；`ref/` 目录为只读参考仓库，**不参与构建、不加进依赖**。

## 代码与测试约定

- 模块 docstring 首行注明蓝本来源（如 `蓝本 ref/PrefixSpan-py (MIT)`）与定制点。
- `mining/` 等算法模块保持纯内存、零 db 依赖，落库由 `services/` 编排（module-layout.md §2）。
- 出口模块（cli / mcp_server / web）不写业务逻辑，只做参数解析 + 调 `services/`。
- 测试黄金样例标注来源；检索质量基准（MRR/Recall@k）后续作为 CI 门禁，不得静默放宽阈值。
- 新增语言支持只加 tree-sitter grammar wheel + `astminer/config.py` 的 LanguageConfig，不改引擎。

## 验证

提交前至少运行一次：`uv run pytest`。涉及检索或挖掘逻辑的改动，须附对应黄金样例或对拍测试。
