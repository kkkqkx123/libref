# astminer — AST 抽取与跨文件调用解析

## 职责

Branch B 的解析底座：以 tree-sitter 为解析引擎，从仓库源码与文档代码块中抽取三类核心事实——API 声明、导入别名、原始调用——再把原始调用经**两遍跨文件解析**升级为带置信度的调用实例，并支撑实体分组、源码切块、代码块语言判定、宏内调用挖掘、清单依赖抽取等外围能力。纯内存代码，零 db 依赖；仓库克隆/缓存键由 services 编排。

## 关键文件

| 文件 | 职责 |
|---|---|
| `engine.py` | 单文件抽取编排：源码字节 → 声明/别名/调用/证据 |
| `parse.py` | Parser 与已编译查询的进程内缓存（按语法指纹失效） |
| `config.py` | 11 种语言的声明式 `LanguageConfig` + 语法/规则指纹 + 启动自检 |
| `queries/` | 每语言四个查询文件（entity/call/dependency/comment）+ 捕获命名契约 |
| `declarations.py` | 实体装配流水线（收集→嵌套→分类→详情→合并→归属→出记录） |
| `calls.py` | 调用记录构建、调用者归属、重叠命中去重、宏调用挂接 |
| `ownership.py` | 限定名归属归一化、作用域内变量别名的 scope 计算 |
| `c_shadow.py` | C/C++ `extern "C"` 守卫的影子源码处理 |
| `context.py` | 文件相对路径 → 模块路径（每语言规则）、Python 相对导入展开 |
| `rules/` | 文本整形（签名合成/参数摘要）、实体命名分类、各语言 import 别名构造 |
| `resolution/` | 两遍调用解析（声明事实 + 别名闭包 → 逐调用判定链） |
| `cache.py` | SQLite AST 增量缓存（按仓库+文件+指纹+配置版本） |
| `repos.py` | dulwich 克隆/拉取、源文件枚举与普查 |
| `grouping.py` / `source_chunk.py` | 实体分组与源码 chunk 装配 |
| `language_evidence.py` / `code_blocks.py` / `snippet_status.py` | 文档代码块的证据、抽取与定级 |
| `macro_calls.py` | C/C++ 宏体与 Rust token_tree 的调用形挖掘 |
| `exports.py` / `manifests.py` / `gradle.py` | 再导出事实、生态清单、Gradle 依赖抽取 |

## 功能清单

### 单文件抽取（engine.py + parse.py）

1. `get_parser` 取（指纹校验后的）Parser；C/C++ 喂入**影子源码**：把 `#ifdef __cplusplus` 守卫的 `extern "C" {` 与 `}` 花括号行替换为等长空格——字节跨度不变，树索引仍指向原文，避免 `#if` 分支留下孤儿花括号破坏树结构。
2. 编译四个 tree-sitter 查询（entity/call/dependency/comment）各取一遍 matches。
3. 产出 `ApiDeclRecord`（声明）、`ScopedAlias`（别名）、`RawCall`（原始调用）、`ParseEvidence`（`has_error` 与各类计数）。

**指纹失效**：`grammar_fingerprint` = SHA1(树蛇运行时版本, 语法 wheel 版本, 配置版本, 查询指纹, 后缀表, 规则文本摘要)。规则文本摘要对 engine/c_shadow/context/rules 五个文件源码做 SHA1——**抽取逻辑改动即失效全部缓存**。Parser 与编译查询以 (语言, 指纹) 为键缓存。

### 声明流水线（declarations.py）

1. **收集**：合并查询匹配；Rust `impl` 块不生成实体而记录为 impl 锚点；名字提取三级回退（`<root>.name` → `.signature.name` → 任意 `.name`）。
2. **嵌套**：用单调包含栈为每个实体确定结构父级。
3. **分类**：根捕获映射 kind；function 且结构父级是类族 kind 则升级 method；class 含 interface/enum 子节点则改判。
4. **详情**：`compose_signature`（signature 捕获 → 头部原文 → 整节点文本，超长截断）；`has_doc` 判定体首字符串表达式（docstring）、区间内注释、或紧贴节点前的注释（bisect 找最近注释终点）。
5. **条件与基类附着**：预处理条件归属与其 span 相交的最小实体；`extend/implement` 文本归入完全包含它的最小实体的 bases。
6. **部分声明合并**：C/C++ 头/源分载的同名 class/struct 按 `(结构父, 短名)` 合并，span 取并集。
7. **归属归一化**：顶层 file_scoped 实体唯一时做"文件所有者"；有 receiver 捕获或落在 impl 锚内的 function 升 method 并挂到 `{module}.{owner}`；其余沿结构父链递推限定名。
8. **C/C++ ERROR 原型恢复**：GNU 属性使 function_declarator 落入 ERROR 节点时，遍历 ERROR → 找祖先 declaration → 文本以 `;` 结束 → 从 ERROR 子树取名与参数，避免声明丢失。

### 调用构建（calls.py）

- 节点类型必须在 `CALL_NODE_TYPES` 内；排除 `import()/require()`。
- `extract_call_parts` 四级规则链取 `(receiver, func)`，拼出 `raw_callee`。
- 实参做**有损摘要**：字面量截 48 字符、标识符/点路径取文本、容器 `[...]`、关键字 `name=value`、嵌套调用 `fn(...)`、未知 `?`。
- caller 归属：完全包住调用点的最内层 function/method 声明限定名。
- **去重**：多查询模式可能命中同一调用点；按 (file, caller, start) 排序线性合并，"尾部函数名相同 + 大小相近 + 重叠 ≥80%"视为重复，保留关系优先级高者。
- **宏内调用**（macro_calls.py）：不做预处理展开。C/C++ 用探针片段 `void __libref_macro_probe(void) { <body> }` 真解析出调用（结果缓存），解析不到的 `name(` 形态正则兜底；Rust 从 token_tree 找"identifier + 后随 `(` 开头的 token_tree"。每个宏调用带宿主宏名、嵌套深度、外层条件链。

### 两遍调用解析（resolution/）

**第一遍（resolve_calls 编排）**：从声明集合建"声明事实"（parent、参数名/个数、param→type、bases）；分出每文件 import 表与作用域变量表；**跨文件闭包**——`__init__.py` 的 re-export 闭包（循环至不动点）+ 别名链展开（`a→b, b→c ⟹ a→c`，回自身即停），迭代到映射快照不变；`effective_for(path, caller)` 合并别名时按调用者祖先链排序作用域变量，越内层越后覆盖。

**第二遍（resolve_one）**：对每个 `raw_callee` 走判定链，输出 `(callee, confidence, resolve_kind)`：

1. 归一化 `::`/`->` 为 `.`；修复黏连的 `newXxx` 头。
2. 已知内置裸调用（同文件外）→ `builtin`/INFERRED（平台自有、身份唯一不算歧义）。
3. 裸名被调用者**参数遮蔽** → `param-shadowed`/AMBIGUOUS。
4. **别名展开**（最长前缀优先）：命中声明 → `alias-direct`/EXTRACTED；结果是标准库 → `stdlib`/INFERRED；**工厂链改写**——prefix 是唯一声明且返回类型唯一解析为 owner 时改写为 `owner.rest`（≤8 步）；**继承 BFS**——沿基类图逐层展开，某层唯一命中 `base+suffix ∈ declared` → `inherited`/EXTRACTED（同层多命中失败，seen 防环）；都不中 → `alias-external`/INFERRED。
5. `self/cls/this/Self` 头：caller 最外层段替换 → `self-direct` / `self-inherited`。
6. **接收者类型推断**：沿父链查 head 的参数类型注解 → `receiver-type`。
7. **候选消歧**：后缀索引（每个限定名登记为其全部点后缀的桶）先按实参个数过滤，再有 receiver 时做"防劫持"（只保留 owner 精确/前缀/父级匹配的候选）；同文件唯一 → `same-file`/EXTRACTED；调用者作用域链收窄到唯一 → `scope`；无法消歧但与别名目标一一对应 → `import-preferred`；否则 `unresolved`/AMBIGUOUS。
8. 收尾：非受保护名的标准库判定覆写；宏产生/宏关系且 EXTRACTED 降为 INFERRED。目标 API 状态（`resolved_target / prefix_candidate / version_unconfirmed / not_target`）按源角色、命中与置信度计算，strict 模式下前缀命中降级。

置信度三档：`EXTRACTED`（直接命中/同文件/别名直击）、`INFERRED`（推断）、`AMBIGUOUS`（unresolved/参数遮蔽）。

### 语言证据与代码块（language_evidence.py / code_blocks.py / snippet_status.py）

- 证据优先级：**显式围栏标签 > 祖先元素 class > 邻近文本 > 配置规则**；`canonical_language` 别名表（py/jsx/cc/golang…）归一；邻近文本判定中 `go` 这类普通词必须出现在 ±24 字符含 code/example 等上下文词的模式里才算，防误判；全部落空时返回候选元组交上层试解析。
- `fenced_blocks`：围栏须同字符且不少于开栏长度才闭合；每块记录宿主页行/字节定位，并截取前文所有 import 形态行（去 `>>>`/`$` 提示符后匹配）为 `import_context`（≤32 行），供解析时提供别名环境。`rust_doc_blocks` 剥 `///`/`//!` 前缀后复用围栏逻辑。
- `classify_snippet_status` 判定序：候选多 → ambiguous；无标签 → unlabeled；语言未配置 → unsupported；无证据 → failed；命中目标调用 → 有 ERROR 则 partial 否则 ok；仅非目标调用 → non_target。**ERROR 节点永不使有效样例变 failed**，只能把有证据的降为 partial。

### 分组与源码切块（grouping.py / source_chunk.py）

- `build_groups`：锚点 = 顶层声明；类族/package 锚沿 children BFS 吸收全部传递后代；孤儿补成单员组；首组带 `header_context`（文件头注释/license）。
- `build_source_chunks`：成员按 span 切原文；超预算的实体 `split_lines_by_cost` 按行成本切多片；同组相邻碎片（左片低于 min_limit、两侧无独立文档、合并不超预算）合并；同 chunk_path 流上挂 `overlap_from` 前片 id；keywords 由限定名及蛇形变体生成（≤5 个）。

### 缓存与仓库管理（cache.py / repos.py）

- `ast_cache(repo, path, fp, cfg_version)` 唯一键 + JSON records；解码严格校验（缺身份字段即抛，防脏缓存）；`sweep_repo` 分批删除与存活映射不符的陈旧行。
- 枚举跳过符号链接、超 1MB 文件、test/tests 目录、`test_*`/`*.min.*` 等；`RepoManager.ensure`：先克隆到临时 staging（dulwich shallow clone）再原子 replace，指数退避重试 3 次；结果登记进 `repos` 表。
- **清单与构建脚本**：`manifests.py` 轻量解析 go.mod/Cargo.toml/package.json/pyproject/pom.xml 为 `ManifestDep`；`gradle.py` 用 groovy/kotlin 语法查询识别 `implementation/api/...` 配置的 Maven 坐标，解 platform() 包装并与 `libs.versions.toml` 版本目录 join；`exports.py` 正则提取 JS `export * from`、Python `__all__`（`ast.literal_eval` 安全求值）、Rust `pub use`。

## 语言配置

`LanguageConfig(name, grammar, file_suffixes, config_version, language_fn)` 声明式配置，11 种语言：c、cpp（**`.h` 归 C++**，C 语法解析头文件会丢成员且 C++ 语法兼容 C）、python、java、javascript、typescript（`language_fn="language_tsx"` 超集）、rust、go、csharp、kotlin、groovy（`.gradle` 纳入使构建脚本进入语料）。`check_config` 启动自检：后缀合法、语法可加载、四查询可编译、捕获校验。

**捕获命名契约**：`entity.<category>[.signature.<attr>|.<role>...]`、`call.<category>[...]`、`dependency.<category>[...]`、`comment.line|doc`；`validate_captures` 保证"查询有捕获、代码必有规则"，防止静默丢失。

## 对外接口

`extract_file`/`extract_file_with_evidence`、`resolve_calls`、`build_groups`、`build_source_chunks`、`fenced_blocks`/`language_from_text`/`classify_snippet_status`、`RepoManager`、缓存四函数、`LanguageConfig` 系列、pydantic 记录模型（`ApiDeclRecord`/`RawCall`/`CallInstanceRecord`/`CodeSnippet`/`ParseEvidence`）。

## 依赖与约束

只依赖 tree-sitter 语法 wheel、dulwich、pydantic；被 services（索引/挖掘/文档代码块）消费。新增语言 = 加一个语法 wheel + 一个 `LanguageConfig` + 一个 `queries/<lang>.py`，不改引擎。
