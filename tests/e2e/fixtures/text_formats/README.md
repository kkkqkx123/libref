# 文本格式核查样本说明

本目录存放文本边界标记的人工核查输入，全部离线确定性样本，不联网。

## 文件清单

- `markdown_sample.md`：带标题、围栏代码、表格与链接的示例文档，是页面组装与截断用例的正文来源。
- `snippet_cases.json`：检索摘录输入，覆盖命中高亮、截断省略、空正文。
- `truncate_cases.json`：问答材料截断输入，覆盖围栏内截断、普通截断、无需截断。
- `evidence_cases.json`：序列证据输入，覆盖命中子序列中间隔缺口的渲染。
- `catalog_cases.json`：目录描述输入，覆盖空白折叠规范化。

## 人工核查入口

运行生成器后查看 `tests/e2e/outputs/text_formats/` 下的输出文件：

- `01-snippet.txt`：摘录必须用 `<excerpt>` 包裹，命中用 `<hit>` 标记，省略用 `<gap/>`，不得出现 `>>>` 或 `...` 伪造连续。
- `02-truncate.txt`：截断必须落在行边界且围栏成对，尾部为 `<gap/>`，正文原样不转义。
- `03-page.txt`、`04-section.txt`、`05-window.txt`：整页、整节、窗口必须为 `<doc>` 下的 `<unit>` 序列，每个单元自带块编号与顺序，正文出现一次；明细只给定位与长度，不重复正文；窗口与整页方言一致。
- `06-materials.txt`：问答材料必须为 `<materials>` 下的 `<unit>` 序列，属性带序号、地址与回读定位，正文原样。
- `07-evidence.txt`：证据必须只渲染命中子序列加必要邻居，跳过的调用用 `<gap lines/span/>` 标出，每次调用自带文件、行区间与置信。
- `08-catalog.txt`：描述空白折叠后无连续空行堆积。
- `SUMMARY.txt`：各文件条数与标记统计，核查时逐项对照。

生成器为 `tests/e2e/generate_text_format_outputs.py`，只做参数解析加服务调用，不含业务逻辑。
