# 文档特征题目人工语义核对记录

依据：`tests/e2e/fixtures/doc_features/questions.json`、`tests/e2e/bench/fixtures/*/docs/pages.jsonl.gz`、`tests/e2e/bench/data/bench.db`、`tests/e2e/fixtures/doc_features/drift_samples.json` 与 `docs/plan/doc-feature-question-manual-authoring.md`。本文记录人工对照原始正文与题目后的发现、处置和待完成口径；不把同源一致性写成语义正确率。

## 核对方式

先把每个归档解压到临时目录，逐库抽取题目的目标 URL、标题路径、题面、期望实体和判定，再回原始正文定位同一章节，检查：

- 题面是否来自该章节正文，而不是页面导航、脚本、页脚或元数据。
- 标题路径是否确实对应产生 facet 的那个文档块；同名标题存在多个块时不能以页面第一处匹配兜底。
- 期望实体是否在题面中点名，是否是目标实体而不是恰好同名的普通英文词。
- drift 题是否指向正文中真正出现弃用、移除或替代说明的句子。
- 无答案题的目标块是否确实没有受跟踪 API 边。

程序化标记只用来列可疑项并解压归档，不产出题目；最终语义结论由人工逐题判断。

## 发现的问题

1. 生成器题目中仍有导航和页脚片段。例如 `FV08/09/15/16/19/23/25/26/28/29` 以 `View page source` 开头，`GR18/23/24/25/28/30` 是 `For the complete documentation index, see llms.txt.`，`RK13/14/15/16/30` 来自 `What's next?`、`FAQ` 等目录；`HT11`、`FV13`、`RK01/RK10` 的章节开头同样是菜单。旧记录列出的 `Initializing search`、`On this page` 等短语已被旧标记表挡住，但扩展人工复核仍发现上述残留。
2. 泛词短名仍然入选实体。例如 `FV09` 的 `FluentValidation.Severity.Warning` 来自警告横幅，`VU20` 的 `content` 来自普通短语，`GR23/24/25/28/30` 的 `complete` 来自导航句式，`RK01/RK10` 的 `configure`、`PY21` 的 `wrapped`、`GR14` 的 `sequence` 都是正文普通词。禁用表不能替代逐题语义判断。
3. 22 条 drift 题里 11 条题面没有完整弃用词：`FB15`、`PY13-PY16`、`FV10`、`FV12`、`GR14-GR17`。`GR16` 只截到 `deprec`；`GR17` 的 `ADDED, REMOVED or MODIFIED` 是枚举值，不是 API 移除；`GR14` 属于通用升级建议。
4. 71 条无答案题期望实体全为空，但按精确标题路径回查 `doc_api_links`，其中 53 条目标块实际有受跟踪边，例如 `HT12` 有 15 条、`ZD14` 有 31 条。空实体来自题面缺少别名，不是没有边。
5. 题面与 facet 可能来自不同块。`ZD07` 的表格参数来自 `@traversable/zod-test` 等表项，题面却取到正文末尾目录 `Powered by Zod Zod Utilities`；`HT07` 标为参数题但题面来自 `Authentication` 示例；`JS07` 标为表格题但题面来自代码块；`FB10` 的 `44.616405` 是日志时间戳而非版本号。同名标题、正文截断和目录跳过规则共同造成这类错配。
6. 34 条题面达到 180 字上限，其中 26 条在字母或数字中间截断，例如 `PY14` 的 `ConfigDic`、`GR16` 的 `deprec`。机械截断破坏可读性，也让 drift 句子失去完整语义。

## 处置

- 删除 `tests/e2e/materialize_doc_feature_questions.py` 及其单元测试，不再从归档自动生成题目；候选生成器只作探索，不进入最终判定集。
- 冻结归档题目已按 `docs/plan/doc-feature-question-manual-authoring.md` 分项目逐题手写替换；生成器不再参与最终判定集。
- 手写题必须直接引用所在章节的句子；题面、标题路径、实体、facet、判定和节点都指向同一个文档块。
- 实体必须是题面点名的目标 API；无合格实体时写空数组。无答案题只选目标块没有 `doc_api_links` 的页面。
- drift 题面必须包含正文中的弃用、移除或替代句；枚举值、通用政策和否定式问答不写为 drift。
- 不设机械题面截断；同一 URL 的题目保持在同一 dev/test 划分。
- `doc_feature_alpha=0` 不变；分项目替换完成前不写检索增益门禁。

## 复核结果

- 结构检查：九个真实站点各 30 条，共 270 条，dev/test 各 15；`demo` 保持 12 条；编号和锚点格式完整。
- 机械检查：标题可在原始正文定位 270/270；实体限定名或短名可在标题或题面中找到 270/270；已知人工 `not_drift` 锚点被重新物化 0 条；drift 题的目标章节含弃用词 22/22。
- 替换前语义检查：扩展导航特征命中至少 25 条；drift 题面缺完整弃用语义 11 条；无答案题的目标块有受跟踪边 53 条；截断题面 34 条；泛词实体和题面与 facet 错配均有实例。旧记录中的“导航命中 0、drift 题面无弃用语义 0、实体不在题面 0”只在生成器自身规则下成立。
- 替换后复核：九个真实站点仍各 30 条、dev/test 各 15；导航与页脚样板题 0；180 字截断题 0；drift 题面缺弃用语义 0；无答案题带受跟踪实体边 0；期望实体不在题面 0；实体存在于目标文档与源码实体版本、锚点/facet/判定/节点与派生证据一致的检查通过。
- `doc_feature_alpha=0` 不变。本轮完成的是人工逐题替换与同源证据复核，独立第三方语义复核仍应在门禁决策前进行。
