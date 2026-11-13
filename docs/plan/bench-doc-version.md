# 文档版本标注：取证、比对与生产侧补法

源码分支已经钉在 `ref` 上并记录了解析到的提交，文档分支却只有 `version: "docs"` 这个夹具作用域
标签。同一项目的两条分支看起来都"有版本"，实际可能描述不同发布，跨分支查询（文档叫 A、源码里
叫 B）的结论就建立在未经验证的假设上。本轮把文档版本变成可验证的事实，并说明生产侧如何补同样
的能力。

## 一、现状：生产有版本字段，但没有版本证据

- 存储层：`pages.version_id`、`documents.version_id` 都挂在 `versions` 行上，版本来自注册表解析
  （`services/registry/resolver.py` 的 `_pick_version`，优先取 `stable`）。
- 抽取层：`crawler/extract.py` 已经解析 `<link rel="canonical">`，但只用于净化流程，没有落库
  （`documents.metadata` 只有 `title`/`path`）。
- 结论：生产是"认定"这批文档属于版本 X，从不核对站点自己怎么说；注册表 stable 与站点实际
  文档版本可以长期不一致而无人发现。

基准侧同样如此：文档夹具的 `version` 是夹具标签（`docs`），不是库版本。

## 二、版本证据从哪里取

站点几乎不声明精确发布号，多数只给"流"（`latest`/`stable`）或"带"（`v3.x`）。因此证据按可靠性
排序采集，取到即停：

| 顺序 | 信号 | 说明 | 实测 |
|---|---|---|---|
| 1 | 抓取到的页面路径段 | 版本化站点把版本放在路径里，是"这批语料实际是什么"的直接证据；也能识别 `latest`/`stable` 这类跟随主干的流 | FluentValidation `/` → `/en/latest/`；pydantic 文档在 `/docs/validation/latest/` |
| 2 | 页面的 canonical 地址 | 站点自己的声明，抗别名 URL；判据是路径里出现版本样式的段 | httpx 的 canonical 就是无版本首页 |
| 3 | 站点发布的版本元数据 | 各家不同：Docusaurus `<meta name="docsearch:version">`，Sphinx 的 `documentation_options.js`（`VERSION`/`RELEASE`），Material 的版本选择器 | gofiber `content=v3.x`；FluentValidation 的 Sphinx `VERSION` 是空串 |
| — | 注册表 stable | 不是站点声明，是外部代理值；只能用来判断"站点 v3.x 与注册表 3.7.0 是否自洽"，不能当证据使用 | 可离线取 |

都没有 → 直接记 `unversioned`，不去猜。允许"未标注"是这套设计的关键：多数站点的诚实答案就是
"它不声明版本"。

## 三、基准侧实现

- **规格**：文档条目新增必填 `doc_version`，取值是站点声明的原样字符串（`2.9`、`v3.x`、
  `latest`）或 `unversioned`。它进入获取配方，改它不重新获取会在校验步骤被拦下。
- **归档元数据**：`content.doc_version` 记录 `{value, signal, sample_url}` —— 观测值、命中的信号、
  采样页地址。没有证据时 `value=unversioned`、`signal=none`。
- **获取时比对**：声明的版本必须等于观测值，否则获取失败，报错给出观测值、信号与采样页，
  让人一眼看出是"站点变流了"还是"规格写错了"。失败不落半个归档。
- **报告**：夹具身份行并列"文档版本（信号）"与源码提交，随时能看出这对语料是"文档在 latest、
  源码在某个提交"还是"文档与源码同一发布"。
- **当前语料**：`httpx` 文档站无版本路径、canonical 指向首页、无版本元数据，因此标注为
  `doc_version: unversioned`（信号 `none`），与源码分支的 `head` 恰好自洽。归档正文未变，
  修订仍为 `6ca0d3d66d43`，只重写了元数据。

判定"版本样式"的规则保持窄：路径段是 `v?数字[.数字[.数字]][.x]` 或 `latest`/`stable`/`dev`/
`main`/`master`/`next`/`current` 之一，且必须被至少八成页面共享，避免把某页自己的 `v1` 当成站点版本。

## 四、生产侧怎么补

生产要补的是"**站点声明**"这一事实，而不是再写一个标签。分四步，风险逐级升高：

1. **采集**：`crawler/extract.py` 的 `ScrapeResult` 增加站点版本字段，取自 `<meta name=
   "docsearch:version">`、`<meta name="version">`，以及 canonical 路径里的版本段。这一层是纯函数，
   没有额外请求，成本为零。Sphinx 的 `documentation_options.js` 需要额外一次 GET，先不做，
   等确有必要再对索引页按需抓取。
2. **落库**：`documents.metadata` 是逐页重建的，不适合放全局事实。放版本级：`versions` 表增加
   `docs_version_label` 与 `docs_version_signal`，由 `services/docs/ingest.py` 在抓取收尾时按页
   多数票写入（与基准侧同一套规则：路径段优先，其次 canonical，再次元数据，都没有记 unversioned）。
3. **比对与暴露**：注册表解析出的版本是检索作用域的唯一依据，**不因站点声明而改变**；站点声明只做
   溯源与告警。三条用途：
   - 搜索答案里可带上"文档标注为 latest，可能描述比 2.14.3 更新的接口"；
   - 站点从 `latest` 切到具体版本（或反之）时，同一 `versions` 行下的语料含义变了，应触发该版本
     的重抓与向量重建；
   - 注册表 stable 更新而站点仍声明旧版本时，提示"文档滞后"。
4. **展示**：`libref show`/`libref search` 的版本行把注册表版本与站点声明并列，避免用户把
   "docs 说 2.14.3" 当成"站点说 2.14.3"。

明确不做的：不把站点声明写进检索作用域（Qdrant payload、向量版本过滤、`repo://` 地址都继续用
注册表版本），否则版本过滤的语义会随站点措辞漂移；也不为取不到的站点编造版本。

## 五、验收

- 规格缺 `doc_version` 的文档条目必须被拒绝；声明与观测不一致时获取必须失败且报错含观测值。
- 文档夹具的报告身份行必须出现"文档版本（信号）"；无证据时显示 `unversioned（none）`。
- 现有语料重建、评测与门禁结论不变：页数、块数、方法矩阵与改造前逐项一致。
