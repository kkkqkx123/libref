# 参考 cce-parser 的解析能力补充计划

本文是下一阶段源码解析链路的改造任务说明。目标不是复制 `code-context-engine` 的整体架构，而是从 `cce-parser` 中选取能直接提升 libref API 使用挖掘质量的能力，按职责补进现有 `astminer` 与 `services/code` 模块。

本计划的前提是：libref 已完成用法语料与 API 定义来源的分离，默认 `usage` 档案排除库内部实现。因此本阶段只增强“如何解析语料”和“如何标注调用事实”，不再改变语料策略。

## 一、参考项

以下路径均指向 `~/code/code-context-engine`：

- C/C++ 头文件解析问题与修复结论：`docs/issue/cpp-header-parse-errors.md`
- C/C++ 字节长度不变的 shadow 源码：`crates/parser/cce-parser/src/parser/cpp_interop_guard.rs`
- C 语言查询方案：`crates/parser/cce-parser/src/tree_sitter_query/scheme/c.rs`
- C++ 语言查询方案：`crates/parser/cce-parser/src/tree_sitter_query/scheme/cpp.rs`
- 宏体抽取：`crates/parser/cce-parser/src/parser/extractor/macro_body_extractor.rs`
- 调用关系抽取：`crates/parser/cce-parser/src/parser/extractor/relation_extractor/call_extractor.rs`
- 调用去重：`crates/parser/cce-parser/src/parser/extractor/relation_extractor/call_extractor.rs`
- 标准库分类入口：`crates/parser/cce-parser/src/parser/extractor/post_processing/stdlib_classifier.rs`
- 标准库检测统一入口：`crates/parser/cce-parser/src/parser/stdlib.rs`
- C 标准库表：`crates/parser/cce-stdlib/src/c.rs`
- C++ 标准库表：`crates/parser/cce-stdlib/src/cpp.rs`
- Rust 标准库表：`crates/parser/cce-stdlib/src/rust.rs`
- 单文件调用解析：`crates/parser/cce-parser-core/src/local_call_resolver.rs`
- 嵌入代码块解析与 span 映射：`crates/parser/cce-parser/src/parser/extractor/embedded.rs`
- 嵌入块类型：`crates/parser/cce-parser/src/parser/embedded_types.rs`
- 测试标记检测说明：`docs/parser/grouper/test_detection.md`
- 测试标记检测实现：`crates/parser/cce-parser/src/grouper/recognizers/test_suite/`
- 签名特殊处理：`docs/parser/tree-sitter-query/signature-special-handling.md`

## 二、当前差距

libref 当前已有 Rust、Go、C#、C/C++ 等语言配置和通用两遍解析链，但与 `cce-parser` 相比存在以下差距：

1. `.h` 当前按 C 解析，容易把 C++ 头文件中的类、方法、成员误识别为自由函数或全局变量。
2. C/C++ 公共头文件中常见的 `extern "C" { ... }` 守卫会让 tree-sitter 的条件预处理结构错位，产生解析降级。
3. C/C++ 查询只覆盖最小函数、结构体、调用和 include，未覆盖宏定义、函数式宏、预处理条件、函数指针、typedef 与函数引用。
4. 宏包装 API 与宏参数中的调用会漏掉，影响 DPDK 这类宏密集库的用法特征。
5. 标准库识别目前主要依赖 Python 标准库名和少量前缀，C/C++/Rust 等语言的标准库调用容易进入用法统计。
6. `resolve_kind` 表示解析来源，不表示调用形态，无法区分直接调用、指针调用、宏调用、回调和函数引用。
7. 文档代码块和 Rust doctest 使用合成路径解析，证据不能精确映射回原始页面或源文件的行范围。
8. probe 只能按目录和文件名辅助判断，缺少基于 AST 标记的 test/example 候选信号。

## 三、任务一：修正 C/C++ 头文件语言映射

### 目标

让 `.h` 按 C++ 解析，`.c` 继续按 C 解析。

### 依据

`cce-parser` 的真实项目统计表明，C++ grammar 是 C query 的超集，纯 C 头文件按 C++ 解析不丢实体；`.h` 仅凭后缀无法判断是 C 头还是 C++ 头，按 C 解析会误判 C++ 符号。`.c` 不应改判 C++，因为反向验证会略增错误。

### 改动范围

- `src/libref/astminer/config.py`
- `docs/features/language-coverage-matrix.md`

### 验收

- `.h` 由 `CPP_CONFIG` 处理，`.c` 仍由 `C_CONFIG` 处理。
- C 头文件与 C++ 头文件的最小夹具均能产出预期实体。
- 语言覆盖矩阵同步更新后缀说明。

## 四、任务二：移植 C/C++ shadow 源码处理

### 目标

在 tree-sitter 解析前生成字节长度不变的 shadow 源码，中和成对 `extern "C"` 守卫，同时保留原始源码用于证据展示。

### 依据

参考 `cpp_interop_guard`。其核心约束：

- 只处理成对守卫，必须同时找到开闭两半。
- 替换内容与原内容字节数一致，行号、列号、偏移不变。
- 条件指令保留原位。
- 未识别或不平衡时原样交给 grammar。
- shadow 只喂给 parser，原文继续作为文档和证据来源。

### 改动范围

- 新增 `src/libref/astminer/c_shadow.py`
- `src/libref/services/code/repo_collect.py`
- `src/libref/services/code/usage_ingest.py`

### 接口形态

提供一个纯函数，输入原始 bytes 与语言名，输出解析用 bytes。函数只处理 C/C++，其他语言原样返回。

### 验收

- 含 `extern "C"` 的头文件解析不再产生大跨度预处理节点。
- 调用与实体的 line/byte span 仍能定位原文。
- 不平衡守卫或未知形态不改变源码。
- 覆盖 CRLF、缩进、`#if defined(__cplusplus)` 写法。

## 五、任务三：扩展 C/C++ 查询

### 目标

补齐 C/C++ 查询中对 API 使用挖掘有价值的结构。

### 依据

参考 C/C++ scheme。优先补齐：

- `preproc_def` 与 `preproc_function_def`
- `#ifdef`、`#ifndef`、`#if defined(...)`
- 普通函数原型与指针返回函数
- 函数指针声明与函数引用
- 结构体、联合体、枚举、typedef
- C++ 类、方法、构造函数、析构函数
- 直接调用、成员调用、函数式宏调用

### 改动范围

- `src/libref/astminer/queries/c.py`
- `src/libref/astminer/queries/cpp.py`
- `src/libref/astminer/queries/__init__.py`
- `src/libref/astminer/config.py`
- `docs/features/language-coverage-matrix.md`

### 约束

- 不照搬 cce 的全部 capture 名称，只保留 libref 当前数据模型可消费的 capture。
- 新增根实体必须在 `rules/entities.py` 有选择规则，否则查询校验会失败。
- 每次升级配置版本或查询文本，语法指纹自然失效缓存。

### 验收

- C/C++ 查询全部通过 `check_config`。
- 宏定义、函数式宏和预处理条件出现在抽取结果中。
- 宏调用与原相对路径、行号可追溯。
- 函数指针或函数引用至少有一组差分用例。

## 六、任务四：宏体与宏内调用提取

### 目标

补上宏包装 API 导致的调用漏抽，但明确不做完整预处理器展开。

### 依据

- Rust 宏体和宏参数内调用参考 `macro_body_extractor.rs` 与 `call_extractor.rs`。
- C 宏按查询捕获定义和直接调用；简单宏体扫描只作为低置信补充。

### 当前限制

`cce-parser` 也不做完整宏展开。它能抽宏体事实和部分宏内直接调用，但不还原宏展开后的间接调用。libref 应记录该限制，不把低置信宏体调用等同于直接 API 调用。

### 改动范围

- `src/libref/astminer/queries/rust.py`
- `src/libref/astminer/queries/c.py`
- 新增 `src/libref/astminer/macro_calls.py`
- `src/libref/astminer/models.py`
- `src/libref/astminer/resolution.py`
- `src/libref/db/schema.py`
- `src/libref/db/migrations.py`
- `src/libref/db/postgres_schema.py`

### 数据模型

`call_instances` 增加调用形态字段，宏体来源调用标记为宏形态并保持较低置信。若暂不新增字段，可先用 `resolve_kind="macro"` 过渡，但不与标准库/source role 混用。

### 验收

- Rust `println!("{}", run())` 能补出 `run()` 调用。
- Rust `macro_rules!` 体内已知函数能被记录。
- C 函数式宏调用能被识别为宏调用。
- 宏调用有独立置信与来源，默认可被 mining 过滤或放行。
- 文档明确说明不支持完整宏展开。

## 七、任务五：标准库识别前置

### 目标

把标准库判定从解析后零散规则前移到抽取/解析阶段，并覆盖主要语言。

### 依据

参考 `cce-stdlib` 的语言表和 `stdlib_classifier`。标准库调用不应成为用法特征主成分，尤其 C/C++ 的 `printf`、`malloc`、`std::vector` 等。

### 改动范围

- 新增 `src/libref/astminer/stdlib.py`
- `src/libref/astminer/resolution.py`
- `src/libref/config.py`
- `docs/app/config.md`

### 配置

增加标准库过滤开关与语言表版本配置。默认排除标准库调用，但允许显式放行，沿用审核与 `include_ambiguous` 语义。

### 验收

- C/C++/Rust 标准库调用标记为 `stdlib` 或对应类别。
- 标准库调用默认不进入 usage 档案的序列模式。
- 用户显式要求时可在健康统计与读模型中查看。
- 不误伤与标准库同名的用户 API：目标 API 命中优先于标准库判断。

## 八、任务六：调用关系类型与去重

### 目标

把“调用形态”和“解析来源”拆开，并降低多 query 重叠带来的重复调用。

### 依据

参考 `call_extractor.rs`：

- 调用形态覆盖 direct、method、static、chained、pointer、macro、callback、funcref 等。
- 模块级调用保留为文件级关系。
- 同一调用点可能命中多个 query，需要按 caller、尾段名称和 span overlap 合并。
- 更具体的关系类型优先；同类型保留更短 span。

### 改动范围

- `src/libref/astminer/models.py`
- `src/libref/astminer/engine.py`
- `src/libref/astminer/resolution.py`
- `src/libref/services/code/repo_persist.py`
- `src/libref/services/code/mining_read.py`
- `src/libref/db/schema.py`
- `src/libref/db/migrations.py`
- `src/libref/db/postgres_schema.py`
- `docs/features/db/code-mining.md`

### 验收

- `resolve_kind` 与 `relation_kind` 各自独立。
- 同一调用点不会因多个 query 命中被重复计频。
- file-level 调用仍能形成样本序列。
- 读模型可按关系类型过滤。

## 九、任务七：局部调用解析增强

### 目标

提高参考项目这类外部仓库中的调用解析精度，避免同名本地函数劫持目标库 API。

### 依据

参考 `local_call_resolver.rs`：

- 用 AST 预计算实参数参与候选消歧。
- `self/this` 接收者才允许回落到同文件同名函数。
- 其他接收者应通过类型或导入解析，不能直接绑定同名本地函数。
- 文件级调用保留，不伪造 caller。

### 改动范围

- `src/libref/astminer/resolution.py`
- `tests/integration/test_resolution_scopes.py`
- `tests/integration/test_usage_corpus_mining.py`

### 验收

- 同名本地函数不会抢占 `obj.method()` 或 `Foo::method()`。
- `self/this` 调用仍解析到当前类方法。
- 实参数不同的重载候选能按数量消歧。
- 目标 API 过滤后参考项目只剩目标库调用参与挖掘。

## 十、任务八：代码块 span 保真

### 目标

让文档代码块、Rust doctest 和 HTML 代码块中的调用证据能定位到原始来源位置。

### 依据

参考 embedded parser 的 span 映射思想：块语言解析后，把块内 span 映射回宿主文件，而不是丢失原始位置。

### 改动范围

- `src/libref/astminer/models.py`
- `src/libref/astminer/code_blocks.py`
- `src/libref/crawler/code_blocks.py`
- `src/libref/services/code/usage_ingest.py`
- `src/libref/services/docs/page_pipeline.py`
- `docs/features/db/code-mining.md`

### 数据模型

`CodeSnippet` 增加原始位置字段：

- 来源 URL 或文件路径
- 原始起止字节
- 原始起止行

调用实例落库时把这些位置写入 `caller_file`、`line_start`、`line_end`，必要时增加原始 span 字段。

### 验收

- Markdown 代码块证据指向原页面与围栏行。
- Rust doc comment 证据指向原 `.rs` 文件行。
- HTML `pre/code` 证据指向原页面。
- 不再依赖合成路径作为唯一证据。

## 十一、任务九：测试与示例候选识别

### 目标

让 probe 能给出 test/example-like 候选，但不自动改变语料角色。

### 依据

参考测试标记检测说明与实现：

- AST 注解优先，如 `#[test]`、`@Test`。
- 命名约定必须有文件级约束。
- 路径规则只作为候选信号。
- 无实体信号时不做猜测。

### 改动范围

- `src/libref/services/code/probe.py`
- `src/libref/services/code/repo_collect.py`
- `docs/features/probe-and-scope.md`
- `docs/app/cli.md`
- `docs/app/mcp.md`
- `docs/app/web.md`

### 验收

- probe 输出增加候选角色与命中来源。
- 路径命中、注解命中、命名命中有明确区分。
- 候选只作为建议，最终 `source_role` 仍由显式索引参数决定。
- 测试候选不会自动进入 `official_example`。

## 十二、明确不做的内容

以下内容不纳入本阶段：

- cce-parser 的 `summary`、`ast_to_nl`、BM25/embedding 模板。
- cce-parser 的模型增强摘要与自然语言转写。
- cce-parser 的插件系统与自定义语言 ABI。
- 完整 codegraph 服务与图查询层。
- 完整 C 预处理器展开。
- 基于 LLM 的示例质量判断。

原因是 libref 已确定使用原始源码块、纯静态抽取和声明式 `LanguageConfig`，不应引入与现有设计冲突的模块。

## 十三、实施顺序与验收标准

建议按顺序实施：

1. 修正 `.h` 语言映射。
2. 移植 `cpp_interop_guard`。
3. 扩展 C/C++ 查询。
4. 前置标准库识别。
5. 增加调用关系类型与去重。
6. 补宏体与宏内调用。
7. 增强局部调用解析。
8. 实现代码块 span 保真。
9. 扩展 probe 的候选识别。

总体验收：

- 全量 pytest 通过。
- ruff check、ruff format check、mypy 通过。
- 新增 C/C++ 宏与 extern 守卫差分夹具。
- 新增 DPDK 风格 `rte_*` 宏调用用例。
- 新增文档/Rust 代码块 span 映射用例。
- mining golden 用例覆盖：标准库调用被排除、宏调用可显式纳入、同一调用点不重复计频。
