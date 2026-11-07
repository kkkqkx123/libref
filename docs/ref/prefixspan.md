# PrefixSpan-py 功能分析

> 仓库：https://github.com/chuanconggao/PrefixSpan-py（本地 `ref/PrefixSpan-py`）。MIT，纯 Python，PyPI 包名 `prefixspan`。
> 定位：PrefixSpan（频繁序列模式挖掘）、BIDE（closed 模式）、FEAT（generator 模式）三算法的统一极简实现。本课题序列挖掘模块的**直接复刻参考**——替代已停滞的 pyfim（最后发布 2020-01，wheel 仅到 cp37）。

## 1. 总体结构（核心 ~350 行）

| 文件 | 职责 | 行数 |
|---|---|---|
| `prefixspan.py` | `PrefixSpan` 类：持有 `db`、`minlen/maxlen`、`_mine()` 驱动递归 | ~26 |
| `frequent.py` | 频繁模式挖掘入口（minsup 语义） | ~67 |
| `topk.py` | Top-k 挖掘入口（堆 + 排序 + 上界提前 break） | ~80 |
| `closed.py` | BIDE：closed 判定（isclosed）与剪枝（canclosedprune） | ~57 |
| `generator.py` | FEAT：generator 判定与剪枝 | ~40 |
| `localtyping.py` | 类型别名：`DB / Matches / Occurs / Pattern / Key / Filter / Callback` | ~15 |
| `api.py` | 猴子补丁把 frequent/topk 挂到类上 | ~9 |

外部依赖仅 **extratools** 里的两个小函数：`dicttools.nextentries`（投影数据库构建）与 `seqtools.issubseqwithgap` / `sortedtools.sorteddiff`（generator 判定用）——复刻时内联即可，不必引入该库。

## 2. 核心数据结构与算法

### 2.1 Matches（投影匹配列表）

```python
Matches = List[Tuple[int, int]]   # (序列 id, 该序列中模式最后匹配位置 pos)
Occurs = Dict[int, Matches]       # item -> 该 item 的匹配列表
```

- 递归状态不是"投影后的子库"（经典 PrefixSpan 描述），而是 **matches 列表**：每条序列只保留 `(id, lastpos)`。空间 O(匹配数)，比复制子序列省内存。
- 初始状态：`func([], [(i, -1) for i in range(len(db))])`（pos=-1 表示从头匹配）。

### 2.2 投影数据库构建（extratools `nextentries`）

```python
def nextentries(data, entries):          # entries: 当前 matches
    return invertedindex(
        (data[i][lastpos + 1:] for i, lastpos in entries),
        entries
    )
# invertedindex: 对每条序列的 lastpos+1 起的后缀做一次扫描，
# item -> [(seq_id, pos), ...]，同一序列重复出现同一 item 只保留最靠前的位置
```

- 一次扫描同时完成"枚举可扩展 item"与"计算子模式 matches"两件事——**这是整个实现效率的支点**，返回 `Dict[item, Matches]` 直接供递归使用。
- 注意：同一序列内同一 item 只记第一次出现位置（`l[-1][0] == i` 则跳过），即每个 (pattern-extension, sequence) 对只算一次支持度。

### 2.3 递归框架（frequent_rec）

```
frequent_rec(patt, matches):
    1. len(patt) >= minlen 时 verify(patt, matches)（产出该模式）
    2. len(patt) == maxlen 时返回          ← 显式长度上限，大幅提速
    3. occurs = nextentries(db, matches)   ← 扩展候选
    4. for newitem, newmatches in occurs:
           bound 剪枝（非反单调上界则跳过）→ 递归
```

- `verify`：`sup = key(patt, matches)`（默认 `len(matches)` 即支持度），过 minsup 且通过 filter / closed / generator 检查后产出；支持 callback 流式消费（大库不积攒结果列表）。

### 2.4 Top-k 变体（topk.py）的三个优化

1. **小顶堆维护结果**：结果满 k 个后 `heappushpop`（新 sup ≤ 堆顶即丢弃）；
2. **canpass 动态化**：`sup <= results[0][0]`（堆顶=当前第 k 大）即可跳过——阈值随挖掘动态升高；
3. **扩展 item 按 key 降序排序 + break**：一旦某扩展的上界低于当前阈值即停止本轮所有后续扩展（依赖 bound 反单调性保证正确）。

### 2.5 BIDE closed 判定/剪枝（closed.py）

- 核心是**反向扫描** `__reversescan`：对每个已匹配位置向前找，收集"可以插入而不增加支持度的 item"集合（各序列局部集合求交），非空则存在同频超模式 → 非 closed。
- `isclosed` = 反向（模式内部间隙）+ 正向 `__forwardscan`（模式尾部之后）双向检查，哨兵 `None` 处理首尾。
- `canclosedprune` 只做反向检查，作为递归扩展前的提前剪枝（若子模式必然非 closed 则整棵子树剪掉）。

### 2.6 FEAT generator 判定（generator.py）

- 维护 `occursstack`（递归路径上每层模式的 occurs），检查"删掉第 i 项后支持度是否不变"——不变则非 generator。
- 复刻可暂缓：本课题知识库场景优先 closed（去冗余、模式数少），generator 主要用于分类特征场景。

## 3. 对本课题的借鉴清单（复刻 + 定制）

| 借鉴点 | 定制方向 |
|---|---|
| `Matches` 元组列表作为递归状态 + `nextentries` 一次扫描建投影 | 原样复刻；把 `Dict` 换成按 item 排序的遍历以获得稳定输出顺序 |
| `minlen/maxlen` 显式上限 | API 调用序列场景 maxlen 建议 5~10（长调用序列无知识价值），minlen=2（单项模式由共现统计覆盖） |
| `key/bound` 抽象（默认 `len(matches)`） | **加权支持度**：key 可改为"支持度 × 溯源仓库多样性权重"，bound 保持 `len(matches)` 反单调上界用于剪枝 |
| `filter/callback` 钩子 | callback 直接写入 `seq_patterns` 表（流式落库，避免大结果集驻内存）；filter 可过滤仅命中单一仓库的模式 |
| topk 的堆 + 动态阈值 + 排序 break | 检索侧"每库返回 top-k 模式"直接复用此入口，比全量 frequent 后排序高效 |
| BIDE closed 判定 | 保留：closed 模式数远少于全量模式，入库前先 closed 化去冗余 |
| generator/FEAT | 暂不复刻（场景不需要） |
| extratools 的 `issubseqwithgap`/`sorteddiff` | 内联实现（各 <15 行），去除外部依赖 |

## 4. 复刻注意事项

1. **整数 item 约定**：算法假定 db 为 `List[List[int]]`（item 已归一化为整数索引）——与本课题"调用上下文归一化为整数索引"设计天然一致；限定名→整数 id 的映射表存 SQLite（`api_entities`），挖掘完把 pattern 还原为限定名序列。
2. **无 sqlite-vec 式持久化**：纯内存算法，挖掘是离线批任务（pipeline 阶段），结果写 `seq_patterns` 表，不做在线查询。
3. **CPython 性能**：README 自述 PyPy 快 ~10 倍；本课题保持 CPython，靠 maxlen 剪枝 + closed 模式控制规模即可，必要时把 `nextentries` 热循环改 Cython/Rust 扩展（后置决策）。
4. **该库维护状态**：最后实质更新较早（PyPy 兼容提示停在 2018 前后），无 type-checking CI、`api.py` 用猴子补丁挂方法——复刻时改为常规类方法 + 完整类型注解，不作为依赖引入。
5. **正确性验证**：README 示例（4 条序列、minsup=2 的全量输出）可直接作为单元测试黄金样例；closed/generator 输出同样有现成期望值。

## 5. 与 pyfim 的关系

init-req 六节提到的 PyFIM 是 Christian Borgelt 的 C 实现绑定（fim 模块），PyPI `pyfim` 6.28 停滞于 2020-01、wheel 仅 cp27~cp37。**结论：不引入 pyfim，以 PrefixSpan-py 为蓝本自研复刻并按上表定制**；两者算法语义一致（均基于 Borgelt 系频繁模式挖掘思想），对拍基准改用纯 Python 参考实现。

## 论文相关工作表述

PrefixSpan-py 提供了 PrefixSpan、BIDE、FEAT 算法的统一极简 Python 实现，通过投影匹配列表与单遍扫描投影数据库构建实现高效序列模式挖掘；本课题在其基础上复刻并定制加权支持度、流式落库与 Top-k 检索接口，用于从海量开源仓库中挖掘频繁 API 调用序列模式。
