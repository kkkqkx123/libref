"""Bidirectional mapping between qualified names and integers, with stable ordering for reproducibility."""

from __future__ import annotations


def build_table(names: list[str]) -> dict[str, int]:
    ordered = sorted(set(names))
    return {name: idx for idx, name in enumerate(ordered)}


def encode_sequences(
    str_seqs: list[list[str]], table: dict[str, int]
) -> list[list[int]]:
    return [[table[name] for name in seq] for seq in str_seqs]


def decode_pattern(pattern: list[int], reverse: dict[int, str]) -> list[str]:
    return [reverse[i] for i in pattern]


def reverse_table(table: dict[str, int]) -> dict[int, str]:
    return {idx: name for name, idx in table.items()}
