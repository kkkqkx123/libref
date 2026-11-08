"""自研 PrefixSpan 频繁序列模式挖掘（含 BIDE closed 模式）。

输入约定：db 为 item 已归一化为整数索引的序列库 ``list[list[int]]``
（限定名 -> 整数 id 的映射由 :mod:`libref.mining.normalize` 管理）。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from heapq import heappush, heappushpop

__all__ = ["PrefixSpan"]

DB = list[list[int]]
Matches = list[tuple[int, int]]  # (序列 id, 模式在该序列中最后匹配位置)
Occurs = dict[int, Matches]
Pattern = list[int]
Results = list[tuple[int, Pattern]]
Key = Callable[[Pattern, Matches], int]
Filter = Callable[[Pattern, Matches], bool]
Callback = Callable[[Pattern, Matches], None]


def _next_entries(db: DB, matches: Matches) -> Occurs:
    """构建投影数据库：item -> 各序列 lastpos+1 之后首次出现的 (id, pos)。

    蓝本 extratools.dicttools.nextentries，一次扫描同时完成
    "枚举可扩展 item" 与 "计算子模式 matches"（效率支点）。
    """
    occurs: Occurs = defaultdict(list)
    for i, lastpos in matches:
        seq = db[i]
        for pos in range(lastpos + 1, len(seq)):
            entries = occurs[seq[pos]]
            # 同一序列内同一 item 只记第一个位置：每个 (扩展, 序列) 对只计一次支持度
            if entries and entries[-1][0] == i:
                continue
            entries.append((i, pos))
    return occurs


def _reverse_scan(db: DB, patt: list[int | None], matches: Matches) -> bool:
    """BIDE 反向扫描：各匹配位置向前找，收集可无代价插入的 item 集合。

    蓝本 closed.py.__reversescan；会原地更新 matches（向前收缩匹配位置）。
    返回 True 表示存在与当前模式同支持度的超模式（经插入路径）。
    """
    for previtem in reversed(patt[:-1]):
        closed_items: set[int] = set()
        for k, (i, endpos) in enumerate(matches):
            local: set[int] = set()
            for startpos in range(endpos - 1, -1, -1):
                item = db[i][startpos]
                if item == previtem:
                    matches[k] = (i, startpos)
                    break
                local.add(item)
            if k == 0:
                closed_items |= local
            else:
                closed_items &= local
        if closed_items:
            return True
    return False


def _is_closed(db: DB, patt: Pattern, matches: Matches) -> bool:
    """closed 判定（蓝本 closed.py.isclosed）：无同支持度超模式。

    反向检查带首尾哨兵（尾哨兵一轮等效于"尾部扩展"的超集检查），
    再对真实匹配位置做精确的前向检查。
    """
    # 反向：使用 (i, len(seq)) 起点与 [None, *patt, None] 哨兵
    if _reverse_scan(
        db,
        [None, *patt, None],
        [(i, len(db[i])) for i, _ in matches],
    ):
        return False
    # 前向：模式最后匹配位置之后的 item，各序列求交
    closed_items: set[int] = set()
    for k, (i, endpos) in enumerate(matches):
        local = set(db[i][endpos + 1 :])
        if k == 0:
            closed_items |= local
        else:
            closed_items &= local
    return not closed_items


def _can_closed_prune(db: DB, patt: Pattern, matches: Matches) -> bool:
    """BIDE 剪枝：反向检查（含首哨兵）判非 closed 即整棵子树剪掉。"""
    return _reverse_scan(db, [None, *patt], matches[:])


class PrefixSpan:
    """PrefixSpan 挖掘器（蓝本 prefixspan.py + frequent.py + topk.py）。"""

    def __init__(self, db: DB) -> None:
        self.db = db
        # 定制点 3：面向 API 调用序列的默认长度窗口
        self.minlen = 2
        self.maxlen = 10
        self._results: Results = []

    @staticmethod
    def _default_key(patt: Pattern, matches: Matches) -> int:
        return len(matches)

    def _mine(
        self,
        func: Callable[[Pattern, Matches], None],
        key: Key,
        bound: Key,
        canpass: Callable[[int], bool],
        verify_extra: Callable[[Pattern, Matches], bool],
        closed: bool,
        filter_fn: Filter | None,
        callback: Callback | None,
    ) -> Results | None:
        """公共递归驱动（frequent 与 topk 共用框架）。"""
        self._results.clear()

        def rec(patt: Pattern, matches: Matches) -> None:
            if len(patt) >= self.minlen:
                sup = key(patt, matches)
                if not canpass(sup) and (filter_fn is None or filter_fn(patt, matches)) and verify_extra(patt, matches):
                    if callback is not None:
                        callback(patt, matches)
                    else:
                        self._results.append((sup, patt))
                if len(patt) == self.maxlen:
                    return

            occurs = _next_entries(self.db, matches)
            for newitem in occurs:
                newpatt = patt + [newitem]
                newmatches = occurs[newitem]
                if canpass(bound(newpatt, newmatches)) or (
                    closed and _can_closed_prune(self.db, newpatt, newmatches)
                ):
                    continue
                rec(newpatt, newmatches)

        rec([], [(i, -1) for i in range(len(self.db))])

        if callback is not None:
            return None
        return sorted(self._results, key=lambda x: x[0])

    def frequent(
        self,
        minsup: int,
        *,
        closed: bool = False,
        key: Key | None = None,
        bound: Key | None = None,
        filter: Filter | None = None,  # 与蓝本 API 对齐，刻意遮蔽内置名
        callback: Callback | None = None,
    ) -> Results | None:
        """挖掘支持度 >= minsup 的频繁模式；closed=True 时仅返回 closed 模式。"""
        key = key or self._default_key
        bound = bound or key

        def canpass(sup: int) -> bool:
            return sup < minsup

        def verify_extra(patt: Pattern, matches: Matches) -> bool:
            return not closed or _is_closed(self.db, patt, matches)

        return self._mine(
            lambda p, m: None, key, bound, canpass, verify_extra,
            closed, filter, callback,
        )

    def topk(
        self,
        k: int,
        *,
        closed: bool = False,
        key: Key | None = None,
        bound: Key | None = None,
        filter: Filter | None = None,  # 与蓝本 API 对齐，刻意遮蔽内置名
        callback: Callback | None = None,
    ) -> Results | None:
        """挖掘支持度最高的 k 个模式（蓝本 topk.py：堆 + 动态阈值 + 上界 break）。"""
        key = key or self._default_key
        bound = bound or key
        heap: list[tuple[int, Pattern, Matches]] = []

        def canpass(sup: int) -> bool:
            return len(heap) == k and sup <= heap[0][0]

        def rec(patt: Pattern, matches: Matches) -> None:
            if len(patt) >= self.minlen:
                sup = key(patt, matches)
                if not canpass(sup) and (filter is None or filter(patt, matches)) and (
                    not closed or _is_closed(self.db, patt, matches)
                ):
                    if len(heap) < k:
                        heappush(heap, (sup, patt, matches))
                    else:
                        heappushpop(heap, (sup, patt, matches))
                if len(patt) == self.maxlen:
                    return

            occurs = _next_entries(self.db, matches)
            # 定制/蓝本优化：扩展 item 按上界降序，一旦低于当前阈值即 break
            for newitem in sorted(
                occurs,
                key=lambda item: bound(patt + [item], occurs[item]),
                reverse=True,
            ):
                newpatt = patt + [newitem]
                newmatches = occurs[newitem]
                if canpass(bound(newpatt, newmatches)):
                    break
                if closed and _can_closed_prune(self.db, newpatt, newmatches):
                    continue
                rec(newpatt, newmatches)

        rec([], [(i, -1) for i in range(len(self.db))])

        results = sorted(((sup, patt) for sup, patt, _ in heap), key=lambda x: (-x[0], x[1]))
        if callback is not None:
            for sup, patt in results:
                callback(patt, [m for m in self._matches_of(patt)])
            return None
        return results

    def _matches_of(self, patt: Pattern) -> Matches:
        """按定义重算模式在库中的匹配位置（topk callback 场景用）。"""
        matches: Matches = []
        for i, seq in enumerate(self.db):
            it = iter(enumerate(seq))
            pos = -1
            ok = True
            for item in patt:
                for j, x in it:
                    if x == item:
                        pos = j
                        break
                else:
                    ok = False
                    break
            if ok:
                matches.append((i, pos))
        return matches
