# 探查与发现人工核查

本目录对应探查与发现现状，用生产命令行入口跑真实用例并落盘，供人工对照需求确认。

## 运行

```bash
uv run python tests/e2e/probe-discovery/fetch_probe_discovery.py
uv run python tests/e2e/probe-discovery/fetch_probe_discovery.py --only httpx-code
uv run python tests/e2e/probe-discovery/fetch_probe_discovery.py --skip-network
```

脚本直接调用 `libref.cli.main`，保存的文本与用户所见一致，不另起解析路径。
离线探查用例复用 `bench` 归档源码包，无需联网；探查文档与发现用例需真实网络，
失败记为环境不可用，不算回归。

## 用例

用例定义见 `cases.json`，源码分支引用 `bench` 归档标识：

* `mitt-code`：单文件下限，只看目录普查形状。
* `httpx-code`：中型 `Python` 库，相对导入与装饰器。
* `zod-code`：`TS` 多包子树，路径别名与桶重导出，带排除规则。
* `httpx-docs`：小型英文文档站，复用 `bench` 的起始地址与路径前缀。
* `discover/search-web/search-github/resolve`：与现有端到端用例同查询。

## 核查

* `*.stdout.txt`：命令行原始输出，不二次排版。探查为单行 `JSON`，发现为 `suggestions` 加逐行 `JSON`。离线探查的 `source` 为归档解包的临时目录，每次运行变化，只看其余字段。
* `*.meta.txt`：执行参数、退出码、耗时与标准错误，供判断环境不可用还是回归。
* `SUMMARY.txt`：各用例一行摘要，核查时逐项对照。

探查仓库看有效文件与候选总数、后缀分组、目录分组、目录树、采样与清单依赖是否齐全；
探查文档看发现来源、发现总数与接受数、采样标题与内容类型、出链预览与错误表；
发现看候选数与首条字段是否含生态、名称、版本、文档与仓库地址、来源与排序信息。
