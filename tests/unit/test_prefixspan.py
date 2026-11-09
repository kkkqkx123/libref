"""Tests for the in-house PrefixSpan implementation.

Golden samples from the ref/PrefixSpan-py README (4 example sequences, minsup=2).
closed correctness is checked by definition: no single-extension superpattern with the same support exists.
"""

from __future__ import annotations

from libref.mining import PrefixSpan

# README golden sample database
DB = [
    [0, 1, 2, 3, 4],
    [1, 1, 1, 3, 4],
    [2, 1, 2, 2, 0],
    [1, 1, 1, 2, 2],
]

# README "prefixspan-cli frequent 2 test.dat" output
README_FREQUENT_2 = {
    (2, (0,)),
    (4, (1,)),
    (3, (1, 2)),
    (2, (1, 2, 2)),
    (2, (1, 3)),
    (2, (1, 3, 4)),
    (2, (1, 4)),
    (2, (1, 1)),
    (2, (1, 1, 1)),
    (3, (2,)),
    (2, (2, 2)),
    (2, (3,)),
    (2, (3, 4)),
    (2, (4,)),
}

# README "frequent 2 --closed" output
README_CLOSED_2 = {
    (2, (0,)),
    (4, (1,)),
    (3, (1, 2)),
    (2, (1, 2, 2)),
    (2, (1, 3, 4)),
    (2, (1, 1, 1)),
}

README_TOPK_5 = {
    (4, (1,)),
    (3, (2,)),
    (3, (1, 2)),
    (2, (1, 3)),
    (2, (1, 3, 4)),
}


def support_of(db: list[list[int]], patt: tuple[int, ...]) -> int:
    """Compute support by definition: number of sequences containing this gap-allowing subsequence."""

    def contains(seq: list[int]) -> bool:
        it = iter(seq)
        return all(item in it for item in patt)

    return sum(1 for seq in db if contains(seq))


def is_closed(db: list[list[int]], patt: tuple[int, ...], sup: int) -> bool:
    """closed definition: no single-extension superpattern with the same support exists."""
    for ext in {item for seq in db for item in seq}:
        if support_of(db, (*patt, ext)) == sup:
            return False
    return True


def test_frequent_readme_golden():
    ps = PrefixSpan(DB)
    ps.minlen, ps.maxlen = 1, 1000
    got = {(sup, tuple(patt)) for sup, patt in ps.frequent(2)}
    assert got == README_FREQUENT_2


def test_closed_readme_golden_and_definition():
    ps = PrefixSpan(DB)
    ps.minlen, ps.maxlen = 1, 1000
    got = {(sup, tuple(patt)) for sup, patt in ps.frequent(2, closed=True)}
    assert got == README_CLOSED_2
    for sup, patt in got:
        assert is_closed(DB, patt, sup)


def test_topk_readme_golden():
    ps = PrefixSpan(DB)
    ps.minlen, ps.maxlen = 1, 1000
    got = ps.topk(5)
    assert {(sup, tuple(patt)) for sup, patt in got} == README_TOPK_5
    assert len(got) == 5
    # supports in non-increasing order
    sups = [sup for sup, _ in got]
    assert sups == sorted(sups, reverse=True)


def test_minlen_maxlen_window():
    ps = PrefixSpan(DB)  # custom default minlen=2, maxlen=10
    got = {(sup, tuple(patt)) for sup, patt in ps.frequent(2)}
    assert all(len(patt) >= 2 for _, patt in got)
    assert (4, (1,)) not in got and (2, (1, 2, 2)) in got

    ps2 = PrefixSpan(DB)
    ps2.minlen, ps2.maxlen = 1, 2
    got2 = {(sup, tuple(patt)) for sup, patt in ps2.frequent(2)}
    assert all(len(patt) <= 2 for _, patt in got2)
    assert (2, (1, 3, 4)) not in got2


def test_callback_streams_without_results():
    ps = PrefixSpan(DB)
    ps.minlen, ps.maxlen = 1, 1000
    seen: list[tuple[int, tuple[int, ...]]] = []

    def collect(patt: list[int], matches: list[tuple[int, int]]) -> None:
        seen.append((len(matches), tuple(patt)))

    assert ps.frequent(2, callback=collect) is None  # callback mode returns no list
    assert {(s, p) for s, p in seen} == README_FREQUENT_2


def test_weighted_key_with_anti_monotone_bound():
    # Customization point 4: weighted key (weighted by matched sequence length); bound defaults to the key itself
    # (weighted support is anti-monotone: superpattern match set ⊆ subpattern match set)
    ps = PrefixSpan(DB)
    ps.minlen, ps.maxlen = 1, 1000
    got = ps.topk(
        3,
        key=lambda patt, matches: sum(len(DB[i]) for i, _ in matches),
    )
    # top-3 weights of the README custom-key example: 20/15/15
    assert [sup for sup, _ in got] == [20, 15, 15]


def test_filter_excludes_pattern():
    ps = PrefixSpan(DB)
    ps.minlen, ps.maxlen = 1, 1000
    got = ps.frequent(2, filter=lambda patt, matches: all(i != 0 for i, _ in matches))
    # exclude patterns covering the first sequence: no pattern in the result should appear in DB[0]
    for _, patt in got:
        assert not contains(DB[0], list(patt)), patt


def contains(seq: list[int], patt: list[int]) -> bool:
    it = iter(seq)
    return all(item in it for item in patt)


def test_random_against_bruteforce():
    """Cross-check against brute-force enumeration on random small databases (frequent and closed semantics)."""
    import itertools
    import random

    rng = random.Random(42)
    items = list(range(5))
    db = [[rng.choice(items) for _ in range(rng.randint(2, 8))] for _ in range(8)]

    ps = PrefixSpan(db)
    ps.minlen, ps.maxlen = 1, 4
    mined = {(sup, tuple(patt)) for sup, patt in ps.frequent(3)}

    # brute-force all subsequence patterns of length 1..4
    brute: set[tuple[int, tuple[int, ...]]] = set()
    for length in range(1, 5):
        for patt in itertools.product(items, repeat=length):
            sup = support_of(db, patt)
            if sup >= 3:
                brute.add((sup, patt))

    assert mined == brute

    closed_mined = {(s, tuple(p)) for s, p in ps.frequent(3, closed=True)}
    for sup, patt in closed_mined:
        assert is_closed(db, patt, sup)
        assert (sup, patt) in brute
    # closed coverage: every frequent pattern has a closed superpattern of the same support that was mined
    for sup, patt in brute:
        assert any(
            is_subseq(patt, p) for p in (q for s, q in closed_mined if s == sup)
        ), (sup, patt)


def is_subseq(sub: tuple[int, ...], sup_seq: tuple[int, ...]) -> bool:
    it = iter(sup_seq)
    return all(item in it for item in sub)
