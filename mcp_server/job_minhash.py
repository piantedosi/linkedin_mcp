"""MinHash signatures for near-duplicate job posts.

Copied from cv_test/python_utils/job_minhash.py so the LinkedIn MCP can run
without the CV editor. Word shingles are hashed with a fixed set of
permutations. The Jaccard estimate is the fraction of permutation slots that
agree.
"""

from __future__ import annotations

import hashlib

NGRAM = 4
NUM_PERM = 64
THRESHOLD = 0.5


def tokens(text: str) -> list[str]:
    return [word.casefold() for word in text.split() if len(word) >= 2]


def shingles(words: list[str], n: int = NGRAM) -> list[str]:
    if len(words) < n:
        return [" ".join(words)] if words else []
    return [" ".join(words[i : i + n]) for i in range(len(words) - n + 1)]


def _slot(perm: int, shingle: str) -> int:
    digest = hashlib.blake2b(f"{perm}\0{shingle}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def signature(text: str, *, ngram: int = NGRAM, num_perm: int = NUM_PERM) -> list[str]:
    grams = shingles(tokens(text), ngram)
    if not grams:
        return ["0" * 16] * num_perm
    values: list[str] = []
    for perm in range(num_perm):
        smallest = min(_slot(perm, gram) for gram in grams)
        values.append(f"{smallest:016x}")
    return values


def jaccard(left: list[str], right: list[str]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    same = sum(a == b for a, b in zip(left, right))
    return same / len(left)
