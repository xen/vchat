import re
from collections import Counter
from typing import List, Tuple


def extract_shingles(text: str, k: int = 10) -> List[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < k:
        return []
    return ["\n".join(lines[i : i + k]) for i in range(len(lines) - k + 1)]


def find_repeated_shingles(
    docs: List[str], k: int = 10, min_freq: float = 0.5
) -> List[str]:
    shingle_counts = Counter()
    for doc in docs:
        shingle_counts.update(set(extract_shingles(doc, k)))
    threshold = max(2, int(len(docs) * min_freq))
    return [sh for sh, count in shingle_counts.items() if count >= threshold]


def remove_shingles(text: str, shingles: List[str]) -> Tuple[str, List[str]]:
    removed = []
    for sh in shingles:
        if sh in text:
            text = text.replace(sh, "")
            removed.append(sh)
    return text, removed


def visualize_removed_blocks(removed: List[str]) -> str:
    if not removed:
        return '<div class="text-xs opacity-60">Навигация/boilerplate не обнаружены шинглами.</div>'
    blocks = "".join(
        f'<div class="bg-warning/20 border border-warning rounded p-2 my-1 text-xs">{re.escape(block[:200])}...</div>'
        for block in removed
    )
    return f'<div class="text-xs mb-2">Удалено как навигация/boilerplate (шинглы):</div>{blocks}'
