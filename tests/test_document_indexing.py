from __future__ import annotations

from vchat.document_indexing import (
    content_shingle_set,
    document_content_effectively_unchanged,
    shingle_jaccard_similarity,
)


class FakeDoc:
    def __init__(self, content: str, hash_value: str):
        self.content = content
        self.hash_value = hash_value


def test_shingle_similarity_stays_high_for_single_line_date_change() -> None:
    left = "\n".join(
        ["# Title", "Line 1", "Line 2", "Line 3", "Updated: 2026-05-29"]
    )
    right = "\n".join(
        ["# Title", "Line 1", "Line 2", "Line 3", "Updated: 2026-05-30"]
    )
    similarity = shingle_jaccard_similarity(left, right)
    assert similarity >= 0.9


def test_shingle_similarity_drops_for_real_content_change() -> None:
    left = "\n".join(["# Title", "Alpha", "Beta", "Gamma", "Delta", "Epsilon"])
    right = "\n".join(["# Title", "News A", "News B", "News C", "News D", "News E"])
    similarity = shingle_jaccard_similarity(left, right)
    assert similarity < 0.9


def test_effectively_unchanged_uses_shingle_similarity_for_near_duplicate() -> None:
    previous = "\n".join(
        ["# Title", "Body line 1", "Body line 2", "Body line 3", "Published: 2026-05-29"]
    )
    current = "\n".join(
        ["# Title", "Body line 1", "Body line 2", "Body line 3", "Published: 2026-05-30"]
    )
    document = FakeDoc(
        content=previous,
        hash_value="not-the-new-hash",
    )
    assert document_content_effectively_unchanged(document, current) is True


def test_content_shingle_set_falls_back_to_lines_for_short_text() -> None:
    shingles = content_shingle_set("one\ntwo")
    assert shingles == {"one", "two"}
