from __future__ import annotations

import re
from typing import List

import mmh3


def extract_shingles(text: str, k: int = 10) -> List[str]:
    """Line-window shingles for near-duplicate detection (Jaccard similarity)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < k:
        return []
    return ["\n".join(lines[i : i + k]) for i in range(len(lines) - k + 1)]


def normalize_words(text: str) -> list[str]:
    return re.findall(r"[a-zа-яё0-9]+", text.lower())


def compute_trigram_hashes(block: str) -> frozenset[int]:
    """Return a frozenset of signed 64-bit hashes, one per word trigram in block."""
    words = normalize_words(block)
    if len(words) < 3:
        return frozenset()
    hashes: set[int] = set()
    for i in range(len(words) - 2):
        trigram = f"{words[i]} {words[i + 1]} {words[i + 2]}"
        h = mmh3.hash64(trigram, signed=True)[0]
        hashes.add(h)
    return frozenset(hashes)


def extract_content_blocks(text: str) -> list[str]:
    """Split markdown text into content blocks (sections delimited by headers).

    Blocks with fewer than 3 whitespace-separated tokens are dropped.
    """
    blocks: list[str] = []
    lines = text.splitlines()
    current: list[str] = []

    for line in lines:
        if re.match(r"^#{1,6}\s+", line.strip()):
            block = "\n".join(current).strip()
            if block:
                blocks.append(block)
            current = []
        else:
            current.append(line)

    block = "\n".join(current).strip()
    if block:
        blocks.append(block)

    return [b for b in blocks if len(b.split()) >= 3]


def is_boilerplate_block(block: str, boilerplate_hashes: frozenset[int]) -> bool:
    """Return True if >= 50% of the block's word trigrams are boilerplate."""
    if not boilerplate_hashes:
        return False
    hashes = compute_trigram_hashes(block)
    if len(hashes) < 3:
        return False
    overlap = len(hashes & boilerplate_hashes)
    return overlap / len(hashes) >= 0.5
