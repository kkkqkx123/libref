# P2 源码挖掘链路细化修改方案（分支 B）

> 依据：`docs/architecture/tech-stack.md` §3/§6/§8、`docs/architecture/module-layout.md` §1/§2/§3、
> `docs/ref/graphify.md`、`docs/ref/epicoder.md`、`docs/ref/prefixspan.md`。
> 目标（module-layout §3 P2 行）：最小验证链路 **仓库→调用实例→频次→closed 序列模式落 `seq_patterns`**。
> 范围：`astminer/`、`mining/{frequency,cooccur,normalize}`、`services/ingest.py` 扩展、CLI 两子命令、迁移 004。

## 0. 现状盘点（避免重复建设）

| 已有物 | 状态 | P2 处理 |
|---|---|---|
| `mining/prefixspan.py` | **已实现**（蓝本 PrefixSpan-py 复刻：minsup/topk、closed+BIDE 剪枝、key/bound 加权扩展点、callback 流式） | 不重写，只做编排接入（2.5 执行顺序 + 落库 callback） |
| `api_entities / call_instances / seq_patterns` DDL | 已在迁移 001 落地（含溯源字段与确定性 uid 唯一约束） | 直接使用；不变更既有迁移（迁移可重放语义不回退） |
| `services/jobs.py` | P1 已实现队列+write-through+心跳 | `index_repo` / `mine_patterns` 作为 job kind 复用 |
| `review_status` 表 | DDL 已就绪 | P2 只写入（AMBIGUOUS 调用实例标 pending），工作流归 P3 |

## 1. 交付物清单

| 文件 | 状态 | 内容 |
|---|---|---|
| `pyproject.toml` | 改 | `uv add tree-sitter>=0.24 tree-sitter-python tree-sitter-java dulwich>=0.22`（tech-stack §6 P2；grammar 为 cp310-abi3，3.14 兼容已核实） |
| `src/libref/db/schema.py` | 改 | 迁移 004：`ast_cache`（增量缓存）+ `repos`（仓库登记表） |
| `src/libref/db/migrations.py` | 改 | 追加 `Migration(4, "astminer-cache-repos", …)` |
| `src/libref/astminer/__init__.py` | 新 | 导出 LanguageConfig / extract_file / two-pass / RepoManager |
| `src/libref/astminer/config.py` | 新 | `LanguageConfig` frozen dataclass（字段照 tech-stack §3.2）+ python/java 两个实例 + `grammar_fingerprint()` |
| `src/libref/astminer/models.py` | 新 | 跨包边界 pydantic：`ApiDeclRecord` / `CallInstanceRecord`（§3.2 JSON 形态，line_range+args） |
| `src/libref/astminer/engine.py` | 新 | 通用遍历 `_extract_generic` + `walk_calls`（蓝本 graphify engine.py）：类/函数/导入/调用，产原始限定名+实参+行范围 |
| `src/libref/astminer/resolution.py` | 新 | 两遍走：pass1 声明/导入 facts → pass2 别名解析+接收者推导，置信 EXTRACTED/INFERRED/AMBIGUOUS（蓝本 graphify resolution.py） |
| `src/libref/astminer/cache.py` | 新 | 缓存键 = 文件 sha1 + grammar fingerprint + config schema 版本（蓝本 graphify cache.py） |
| `src/libref/astminer/repos.py` | 新 | dulwich `clone --depth 1` / `fetch`+reset 更新；按扩展名遍历文件、排除 vendor/test |
| `src/libref/mining/normalize.py` | 新 | 限定名 ↔ 整数 id 双向映射（纯 Python，零 db import） |
| `src/libref/mining/frequency.py` | 新 | 层级频次聚合 + top-N 剪枝（EpiCoder `extract_frequency_count.py`/`prune_to_top_n` 改写） |
| `src/libref/mining/cooccur.py` | 新 | 文件级倒排 → (api_i, api_j) 共现计数（仅对剪枝后频繁集计算） |
| `src/libref/services/ingest.py` | 改 | 新增 `index_repo()` / `mine_patterns()` 编排（落库+job 提交） |
| `src/libref/cli.py` | 改 | `index-repo` / `mine` 子命令 |
| `tests/test_astminer.py` | 新 | 黄金 fixture + stdlib ast 对拍 + 缓存增量/失效 + 两遍解析置信 |
| `tests/test_mining_freq_cooccur.py` | 新 | frequency/cooccur/normalize 手工对拍黄金样例 |
| `tests/test_ingest_repo.py` | 新 | fixture 迷你仓库端到端：落表→频次→closed 模式→`seq_patterns` |
| `tests/fixtures/mini_repo/` | 新 | 3~5 个 py 文件的样例仓库（黄金样例，标注手写来源） |

## 2. 设计决策

### 2.1 LanguageConfig：声明式配置，新增语言只加配置不改引擎（graphify models.py 模式）

```python
@dataclass(frozen=True)
class LanguageConfig:
    name: str; grammar: str                    # grammar = wheel 模块名，动态 import
    class_types / function_types / import_types / call_types: tuple[str, ...]
    call_function_field: str                   # "function"
    accessor_node_types: tuple[str, ...]       # 成员调用 a.b()
    receiver_types: tuple[str, ...]            # 接收者类型推导（python/java 开启）
    file_suffixes: tuple[str, ...]             # 仓库文件遍历用
    extra_walk_fn: Callable | None = None      # 语言专属钩子
    config_version: int = 1                    # 缓存 schema 代际
```

- 起步两语言：python（`class_definition/function_definition/import_statement/from_import/call`）、
  java（`class_declaration/method_declaration/import_declaration/method_invocation`）。
- `grammar_fingerprint(cfg)` = sha1(tree_sitter 版本 + Language 描述符 id)——grammar wheel 升级即全量失效缓存
  （tech-stack §3.4 的已验证安全升级路径）。
- 启动自检：节点类型名在 grammar 中不存在则报错（防 wheel 漂移静默漏抽）。

### 2.2 抽取记录与确定性 ID（超越 graphify 的两个差异点）

`CallInstanceRecord`（pydantic，tech-stack §3.2 JSON 逐字对齐）：
`call_id, callee, args, caller_file, line_range[start,end], confidence, repo`。
**定制点**：line_range 与实参列表是 graphify 没有的（ref/graphify.md §8），args 只记录
位置参数字面量摘要 + 关键字名，不求值（避免执行语义引入幻觉）。

- `entity_uid = sha1(version_id \0 qualified_name \0 kind)`；
  `call_uid = sha1(repo \0 caller_file \0 line_start \0 line_end \0 callee \0 args_joined)`。
  表上 UNIQUE 约束天然幂等：重跑 `INSERT INTO … ON CONFLICT(call_uid) DO UPDATE`
  ——graphify ids.py 的冲突盐在此不复制，因语义键（限定名/调用位置）不可能异内容同键。

### 2.3 两遍走与置信分层（graphify resolution.py 模式）

1. **pass1** 逐文件收 facts：类/函数声明（限定名）、`import m as a`、`from x import y as z` 别名表；
   跨文件汇总为全局 fact 表。
2. **pass2** 解析调用：callee 沿成员链自左向右替换别名；Python 变量/Java 字段声明类型做接收者推导
   （仅 `receiver_types` 白名单类型）。
3. 置信分层（graphify 三级标签沿用，作为 P3 审核优先级依据）：
   - `EXTRACTED`：解析到本仓库内声明；
   - `INFERRED`：经 import 唯一定位到外部模块（如 `requests.get`）；
   - `AMBIGUOUS`：多候选或未解析（保留原始限定名），落库时写 `review_status='pending'`。

### 2.4 增量缓存（graphify cache.py 模式）

`ast_cache(path TEXT, file_sha1 TEXT, fp TEXT, cfg_version INT, records JSON, PRIMARY KEY(path, file_sha1, fp, cfg_version))`。
逐文件：命中即复用 records（批量爬大库主要收益点）；未命中解析后写回。
不做 stat 预检优化（dulwich 全量 hash 成本可控，先简单）。

### 2.5 挖掘执行顺序（services 编排，落库不回流 mining）

```
index_repo:  RepoManager 获取 → cache 过滤 → engine+resolution 抽 records
             → 落 api_entities / call_instances（幂等 upsert）
mine_patterns(version):
  1. call_instances（按 repo 集合）→ normalize 限定名↔int id
  2. frequency 层级聚合 + prune_to_top_n（默认 n=50, max_depth=3，EpiCoder 实验值）
     —— 先剪枝控制 PrefixSpan 字母表，防序列爆炸
  3. 按 (repo, caller_file) 行序构建序列 DB：list[list[int]]
  4. PrefixSpan.frequent(minsup, closed=True) + callback 流式落库
     minsup 默认 max(20, 1% × 序列数)（APP-Miner 支持度阈值借鉴，tech-stack §1）
  5. 落库时 int id → 限定名 反归一化；pattern JSON = 限定名序列；is_closed=1
```

cooccur.py 产出共现计数表（内存 dict），当前无消费方——按 overview 设计留作检索
特征与报告，**不进 seq_patterns**，接口返回供 services 决定去留（不过度设计）。

### 2.6 mining 纯内存硬约束（AGENTS.md / module-layout §2.3）

`normalize/frequency/cooccur/prefixspan` 四模块输入输出全为纯 Python 数据结构
（`list[list[int]]`、`dict`），**禁止 import db/store/astminer**；限定名→整数 id 的映射表由
services 从 `api_entities/call_instances` 读出后传入。单测因此可完全离线。

### 2.7 仓库获取（repos.py）

- `RepoManager(db_dir)`：`ensure(name, url, ref="HEAD")` —— dulwich clone（depth=1）或
  fetch+reset 更新；`repos` 登记表记 name/url/当前 commit sha/本地路径（迁移 004）。
- 也接受本地路径源（课题内对拍、私有仓库离线跑）——不 clone 直接遍历。
- 文件遍历：`file_suffixes` 过滤 + 排除 `test/ tests/ *.min.* vendor/`（EpiCoder `get_code_from_dir_without_test` 语义）。

### 2.8 出口与 jobs

- CLI：`libref index-repo <library> <version> <git-url|path> [--update]`、
  `libref mine <library> <version> [--minsup N --topn 50]`；默认同步执行（单机小任务），
  `--async` 时经 `JobManager.submit(kind)` 走队列+心跳。
- MCP 工具面 P2 不扩（module-layout P2 行无新增出口）；检索代码模式工具归 P3 retriever 完成后统一设计。

### 2.9 依赖规则核对（module-layout §2）

`astminer → {db, tree-sitter, dulwich}`；`mining → ∅`；`services → {astminer, mining, db, store}`；
三出口只调 services；crawler 与本分支互不依赖（仅经 db 汇合）；Qdrant 触达点不变。

## 3. 验证标准（对应 module-layout §3 P2 最小验证）

1. **抽取黄金样例**：`mini_repo` fixture 的调用实例（callee 限定名、args、line_range）与手写期望逐条一致；
   Python 文件用 stdlib `ast` 交叉校验 callee 集合，抽样 diff 为空（tech-stack §3.3）。
2. **两遍解析**：`import numpy as np; np.array(...)` 解析为 `numpy.array` 且置信 `INFERRED`；
   仓库内调用 `EXTRACTED`；多候选 `AMBIGUOUS` 且 `review_status` 出现 pending 行。
3. **增量与幂等**：同仓库二次 `index_repo` 全部缓存命中（解析计数为 0）且两表行数不变；
   改 `grammar_fingerprint`（模拟升级）→ 缓存全失效重解析。
4. **频次/共现**：3 文件手工黄金数据 → 层级频次与 top-N 剪枝结果、共现矩阵与手算一致。
5. **端到端（最小验证主线）**：`mini_repo` → `seq_patterns` 含已知 closed 模式
   （pattern JSON 为限定名序列、support 精确、is_closed=1）；`uv run pytest` 全绿（P0/P1 不回归）。
