# 用法语料挖掘核查样本

本目录为 `tests/e2e/render_usage_mining_outputs.py` 提供离线确定性输入，全部样本在临时库中生成，不联网、不访问模型服务。

## 文件清单

- `cases.json`：目标库事实、三类 usage 语料源、挖掘档案以及人工核查期望。

## 核查点

- 来源表：实现、官方示例、文档代码块与参考项目分别注册，角色、选择规则与目标过滤可辨认。
- 档案表：`usage` 为默认全量用法档案；两个具名档案由来源角色显式派生；带过滤的挖掘不改写默认档案。
- 模式集：实现档案含内部调用序列，默认用法档案排除内部调用；官方示例档案不含参考项目 API，参考项目档案不含官方示例 API。
- 证据：每条证据带来源角色、来源键、样本与档案指纹。
- 健康：调用形态分布与宏覆盖率可解释。
- 标签：模式标签携带档案名与指纹，问答提示块重复该身份。

输出到 `tests/e2e/outputs/usage_mining/`，文件逐项对应上述核查点，`SUMMARY.txt` 记录不变量判定。

## 真实仓库

- `repos.json`：DPDK 与 MTL 的固定 tag、仓库地址、参考项目声明的 DPDK 版本及依据；每个角色的完整语料选择规则与基线切片。
- `repos.lock.json`：同步后记录实际 commit 与本地路径；不在常规提交中执行同步。
- 同步：`uv run python tests/e2e/sync_usage_repos.py`
- 回放与导出：`uv run python tests/e2e/run_usage_mining_real.py`
- 用例：`LIBREF_RUN_E2E=1 uv run pytest tests/e2e/test_usage_mining_real.py -q`
- 输出：`tests/e2e/outputs/usage_mining_real/`，对照 `SUMMARY.txt` 与质量门槛逐项核查。

## 判定口径

- 目标命中分三类：`resolved_target` 已解析到目标版本实体，`prefix_candidate` 仅命中名称前缀，`version_unconfirmed` 的版本声明无法与目标版本确认一致。
- 默认 `usage` 档案排除实现角色、前缀候选与版本未确认调用；没有可确认用法时保持空集。
- 两个具名审查档案显式放行前缀候选与版本未确认调用，标签与问答提示块标记来源角色、仓库 tag、commit 和非官方推荐提示；确定性标签文本只作审查占位。
- MTL v23.12 的 CHANGELOG 声明 DPDK 23.07，与目标的 v23.11 不一致，因此调用状态为 `version_unconfirmed`，只进入具名审查档案，不进入默认分布。
- 证据的 `canonical_url` 由仓库地址、commit、相对路径与行范围组成；来源表中的本地路径会被改写为规范仓库地址。
- 健康统计按档案 source_ids 过滤；不同档案的置信分布、解析来源、未解析率与宏覆盖率可不同。
- `08-version-compatibility.txt` 记录版本依据，`09-resolution-coverage-diff.txt` 对比基线与扩展头文件切片的未解析率、前缀率、目标调用数、宏调用数与模式集合，`40-health-comparison.txt` 同屏比较档案健康，`50-quality-gates.txt` 记录可反推的观测值与阈值。

## 完整语料口径

真实仓库用例使用固定提交下的完整角色语料，而不是少量示例文件：

- `implementation`：DPDK `lib/` 下全部源码与头文件，提供目标 API 实体。
- `official_example`：DPDK `examples/` 下全部源码，提供官方示例。
- `reference_project`：MTL 全部源码，提供第三方参考用法。

三类语料按角色完整覆盖各自目录；DPDK 的 `drivers/`、`app/` 等目录不是 API 定义或官方示例语料，输出中作为未覆盖目录显式列出。语料源解析时会加载已持久化的目标库声明，因此示例中的调用可以解析到目标版本实体，而不再只凭前缀判定。宏体只记录可见调用，不做预处理展开。低宏覆盖率与未解析/前缀比例进入 `needs_review`，不直接判失败。

全量角色语料下挖掘使用有界参数（最小支持度 3、长度 2-8、前 50 个模式；报告文件最多列出 50 条），避免支持度为 1 时子序列爆炸；基线差分仍使用原受控切片。
