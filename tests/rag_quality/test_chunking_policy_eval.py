from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobs.crawler import tasks as crawler_tasks
from jobs.embedder import chunking


FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "rag_quality"
    / "chunking_policy_cases.json"
)


class _WordTokenizer:
    def __call__(self, text, add_special_tokens=False, truncation=False, verbose=True):
        _ = add_special_tokens, truncation, verbose
        return {"input_ids": text.split()}

    def decode(self, ids, skip_special_tokens=True, clean_up_tokenization_spaces=False):
        _ = skip_special_tokens, clean_up_tokenization_spaces
        return " ".join(ids)


class _Session:
    def __init__(self) -> None:
        self.added = []

    def execute(self, stmt):
        _ = stmt
        return None

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def expunge_all(self):
        pass


def _cases() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text())


@pytest.mark.parametrize("case", _cases(), ids=lambda item: item["name"])
def test_chunking_policy_eval(case: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chunking, "get_embed_tokenizer", lambda: _WordTokenizer())
    monkeypatch.setattr(crawler_tasks, "get_embed_tokenizer", lambda: _WordTokenizer())

    if case["mode"] == "chunk_document_text":
        chunks = chunking.chunk_document_text(case["content"])
        joined = "\n".join(chunk.text for chunk in chunks)

        assert len(chunks) <= case["expected_max_chunks"]
        for value in case.get("required_text", []):
            assert value in joined
        for value in case.get("forbidden_text", []):
            assert value.lower() not in joined.lower()
        forbidden_kinds = set(case.get("forbidden_kinds", []))
        assert not forbidden_kinds.intersection({chunk.kind for chunk in chunks})
        return

    if case["mode"] == "materialize_page_chunks":
        page = SimpleNamespace(
            id=100,
            source_id=None,
            uri=case["uri"],
            title=case["title"],
            content=case["content"],
            hash_value="fixture-hash",
            meta=dict(case.get("meta") or {}),
            status=None,
            status_error=None,
            raw_content_type=case.get("raw_content_type"),
            raw_content_size=case.get("raw_content_size"),
        )
        session = _Session()

        count = crawler_tasks.materialize_page_chunks(session, page)
        joined = "\n".join(chunk.text for chunk in session.added)

        assert count == case["expected_count"]
        assert len(session.added) == case["expected_count"]
        assert session.added[0].kind == case["expected_kind"]
        assert page.meta["index_policy"] == case["expected_policy"]
        assert page.meta["index_policy_reason"] == case["expected_policy_reason"]
        for value in case.get("required_text", []):
            assert value in joined
        for value in case.get("forbidden_text", []):
            assert value.lower() not in joined.lower()
        return

    raise AssertionError(f"Unsupported eval mode: {case['mode']}")
