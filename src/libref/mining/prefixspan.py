"""In-house PrefixSpan frequent sequential pattern mining (including BIDE closed patterns).

Input convention: db is a sequence database ``list[list[int]]`` whose items are
already normalized to integer indices (the qualified-name -> integer-id mapping
is managed by :mod:`libref.mining.normalize`).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from heapq import heappush, heappushpop

__all__ = ["PrefixSpan"]

DB = list[list[int]]
Matches = list[tuple[int, int]]  # (sequence id, last matched position of the pattern within that sequence)
Occurs = dict[int, Matches]
Pattern = list[int]
Results = list[tuple[int, Pattern]]
Key = Callable[[Pattern, Matches], int]
Filter = Callable[[Pattern, Matches], bool]
Callback = Callable[[Pattern, Matches], None]


def _next_entries(db: DB, matches: Matches) -> Occurs:
    """Build the projected database: item -> first occurrence (id, pos) after lastpos+1 in each sequence.

    Based on extratools.dicttools.nextentries; a single scan accomplishes both
    "enumerate extendable items" and "compute sub-pattern matches" (the efficiency pivot).
    """
    occurs: Occurs = defaultdict(list)
    for i, lastpos in matches:
        seq = db[i]
        for pos in range(lastpos + 1, len(seq)):
            entries = occurs[seq[pos]]
            # Record only the first position per item within a sequence: each (extension, sequence) pair counts support once
            if entries and entries[-1][0] == i:
                continue
            entries.append((i, pos))
    return occurs


def _reverse_scan(db: DB, patt: list[int | None], matches: Matches) -> bool:
    """BIDE reverse scan: scan backwards from each match position, collecting items insertable at no cost.

    Based on closed.py.__reversescan; updates matches in place (shrinking match positions backwards).
    Returns True if a super-pattern with the same support exists (via insertion paths).
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
    """Closedness check (based on closed.py.isclosed): no super-pattern with the same support.

    The backward check uses head/tail sentinels (one tail-sentinel pass is equivalent to
    a superset check for "tail extension"), followed by an exact forward check on the
    real match positions.
    """
    # Backward: start from (i, len(seq)) with [None, *patt, None] sentinels
    if _reverse_scan(
        db,
        [None, *patt, None],
        [(i, len(db[i])) for i, _ in matches],
    ):
        return False
    # Forward: intersect items after the pattern's last match position across sequences
    closed_items: set[int] = set()
    for k, (i, endpos) in enumerate(matches):
        local = set(db[i][endpos + 1 :])
        if k == 0:
            closed_items |= local
        else:
            closed_items &= local
    return not closed_items


def _can_closed_prune(db: DB, patt: Pattern, matches: Matches) -> bool:
    """BIDE pruning: if the backward check (with head sentinel) finds a non-closed pattern, prune the whole subtree."""
    return _reverse_scan(db, [None, *patt], matches[:])


class PrefixSpan:
    """PrefixSpan miner (based on prefixspan.py + frequent.py + topk.py)."""

    def __init__(self, db: DB) -> None:
        self.db = db
        # Customization point 3: default length window for API call sequences
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
        """Common recursive driver (shared framework for frequent and topk)."""
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
        filter: Filter | None = None,  # aligns with the ref/PrefixSpan-py API; shadows the builtin intentionally
        callback: Callback | None = None,
    ) -> Results | None:
        """Mine frequent patterns with support >= minsup; only closed patterns when closed=True."""
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
        filter: Filter | None = None,  # aligns with the ref/PrefixSpan-py API; shadows the builtin intentionally
        callback: Callback | None = None,
    ) -> Results | None:
        """Mine the k patterns with the highest support (based on topk.py: heap + dynamic threshold + upper-bound break)."""
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
            # Customization/blueprint optimization: sort extension items by upper bound descending, break once below the current threshold
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
        """Recompute the pattern's match positions in the database from first principles (used for topk callbacks)."""
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
