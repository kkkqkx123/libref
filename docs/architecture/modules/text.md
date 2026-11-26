# text — 共享文本变换叶子包

## 职责

无数据库、无网络访问的纯函数文本处理包，被 crawler、chunking、store、services 等上层共享。核心设计原则：**索引侧与查询侧共用同一套变换**（CJK 展开、snake 变体），保证词法对称。

## 功能清单与算法

### bm25.py — 索引形态清洗
`clean_text_for_bm25(text)`：FTS 写入前的轻度去噪，逐行处理——图片 `![alt](…)` 保留 alt 文本；链接 `[t](…)` 只留文本；尖括号 URL 与裸 http(s) URL 删除；引号/反引号去除；CJK 连续段经 `expand_cjk_run` 展开后以空格连接；最后压缩连续空行到最多 2 行。保留换行与代码围栏。

### cjk.py — CJK 词法展开
- `CJK_RUN_RE`：匹配连续汉字段（含 CJK 扩展 A/兼容区）。
- `expand_cjk_run(run)`：原文 + jieba `cut_for_search` 分词 + 字符 bigram，按序去重；单字 run 原样返回。索引与查询两侧必须用同一函数，展开结果才可比对。

### embedding.py — 嵌入侧文本
- `strip_markdown_for_embedding`：删围栏行，图片/链接保文本，去标题 `#`、引用 `>`、列表符、行内代码反引号、`**加粗**`、`~~删除线~~`，压缩空行。
- `build_embed_text(title, path, content)`：`"title / path"` 头行 + 剥离后正文。
- `is_embeddable_metadata(meta)` / `EMBEDDABLE_SQL_CONDITION`：`chunk_path == "bm25"` 的块只服务 FTS、不参与嵌入；Python 与 SQL 两种表达是同一规则的镜像，必须同步修改。

### ident.py — 命名风格对齐
- `_CAMEL_BOUND_RE` 识别 camelCase 边界（小写/数字后接大写，以及大写缩写后接"大写+小写"）。
- `ident_snake_form(name)`：camelCase → 小写下划线形，使 FTS/关键字检索能对齐跨命名风格的标识符（如 `DocFacet` 命中 `doc_facet`）。
- `snake_variant(token)`：返回与原 token（小写比较）不同的 snake 形，相同返回空串。

### normalize.py — 归一化与分派
- `normalize_markdown`：行尾空白 + 空行压缩到 2。
- `wrap_source_content(raw, url)`：按扩展名挑语言提示（.py/.java/.go/.rs/.cs/.js/.ts/.jsx/.tsx 等）把源码包进 ``` 围栏，保证 chunking 的围栏平衡不变式。
- `classify_content(content_type, url)`：先 URL 后缀（.md/.mdx→markdown，.html→html，九种源码后缀→source），再 MIME 分派，默认 html。

### query.py — 查询构造
- `query_units(query)`：覆盖度检查用的可检索单元——拉丁 token 小写去重 + CJK 段 jieba 分词（≥2 字；丢单字与 bigram 会使覆盖度判定失真）。
- `safe_fts_query(query)`：每个拉丁 token 生成带引号的原文与 snake 变体，CJK 段逐词 `expand_cjk_run`，全部用 ` OR ` 连接，保证 FTS5 MATCH 语法安全。

### snippet.py — 摘录与标记
- XML 风格标记原语：`gap_tag`（省略行）、`reference_tag`（因预算丢弃正文的指针）、`omitted_tag`（按预算丢弃的尾部单位数）、`unit_tag`/`call_tag`（自定位单元/调用包裹）、`xml_escape_attr`。
- `make_snippet(content, query, radius, cap)`：在第一个查询命中周围取整行窗口（≤cap），命中词按长度降序包 `<hit>` 标记，前后接 `<gap/>`，整体包 `<excerpt>`。
- `truncate_material(text, limit)`：`_fence_aware_cut` 在行边界截断后回溯到围栏成对，尾部补 `<gap/>`。
- `fences_balanced(text)`：围栏行数成对即平衡（chunking/store 共用此判定）。

## 依赖与约束

零内部依赖的叶子包（仅 jieba 一个运行时依赖）；被所有上层导入。任何索引形态（`clean_text_for_bm25`、`build_embed_text`）的修改都必须同时检查查询侧（`safe_fts_query`、`query_units`）是否需要联动，两侧词汇表必须一致。
