"""频次、共现、归一化的手工对拍黄金样例。"""

from __future__ import annotations

from libref.mining.cooccur import cooccur_matrix
from libref.mining.frequency import level_counts, prune_to_top_n
from libref.mining.normalize import (
    build_table,
    decode_pattern,
    encode_sequences,
    reverse_table,
)


def test_level_counts_hand_computed():
    names = ["a.b", "a.b", "a.c"]
    counts = level_counts(names)
    assert counts == {"a": 3, "a.b": 2, "a.c": 1}


def test_prune_to_top_n_per_depth():
    counts = {"a": 10, "b": 5, "c": 1, "a.x": 7, "a.y": 2, "b.z": 9}
    kept = prune_to_top_n(counts, 1)
    assert kept == {"a": 10, "b.z": 9}


def test_cooccur_hand_computed():
    sets = [{"a", "b", "c"}, {"a", "b"}, {"b", "c"}]
    matrix = cooccur_matrix(sets)
    assert matrix == {("a", "b"): 2, ("a", "c"): 1, ("b", "c"): 2}


def test_normalize_roundtrip_sorted_ids():
    names = ["numpy.zeros", "numpy.array", "numpy.array"]
    table = build_table(names)
    assert table == {"numpy.array": 0, "numpy.zeros": 1}
    encoded = encode_sequences([["numpy.array", "numpy.zeros"]], table)
    assert encoded == [[0, 1]]
    assert decode_pattern([0, 1], reverse_table(table)) == ["numpy.array", "numpy.zeros"]
