from __future__ import annotations

import argparse

from jobs.embedder.chunking import ChunkData
from tests.rag_quality import local_db_slice_eval


def test_build_query_filters_by_ids_and_uri_like() -> None:
    sql, args = local_db_slice_eval.build_query(
        page_ids=[1, 2],
        uri_like="%docs%",
        limit=3,
    )

    assert "id = any($1::int[])" in sql
    assert "uri ilike $2" in sql
    assert "limit 3" in sql.lower()
    assert args == [[1, 2], "%docs%"]


def test_summarize_page_counts_chunks_by_kind() -> None:
    page = local_db_slice_eval.PageRow(
        id=10,
        source_id=None,
        uri="https://example.com",
        title="Example",
        content="raw content",
    )

    summary, chunks = local_db_slice_eval.summarize_page(
        page,
        mode="chunker",
        chunker=lambda text: [
            ChunkData(0, 0, 5, f"{text} A", "text", token_count=3),
            ChunkData(1, 6, 12, f"{text} B", "summary", token_count=2),
        ],
    )

    assert len(chunks) == 2
    assert summary["page_id"] == 10
    assert summary["chunk_count"] == 2
    assert summary["kind_counts"] == {"summary": 1, "text": 1}
    assert summary["mode"] == "chunker"
    assert summary["machine_artifact_prefix_chunks"] == 0
    assert summary["machine_artifact_prefix_ratio"] == 0.0
    assert summary["auth_like_page"] is False
    assert summary["auth_like_chunks"] == 0
    assert summary["zero_chunk_nonempty_page"] is False


def test_chunk_noise_stats_counts_machine_artifact_prefixes() -> None:
    chunks = [
        ChunkData(
            0,
            0,
            32,
            '{"isActive":false,"cookieKey":null}\nUseful text',
            "text",
            token_count=3,
        ),
        ChunkData(1, 33, 50, "Useful answer content", "text", token_count=3),
    ]

    stats = local_db_slice_eval.chunk_noise_stats(chunks)

    assert stats == {
        "machine_artifact_prefix_chunks": 1,
        "machine_artifact_prefix_ratio": 0.5,
    }


def test_aggregate_noise_stats_counts_across_pages() -> None:
    stats = local_db_slice_eval.aggregate_noise_stats(
        [
            {
                "chunk_count": 2,
                "machine_artifact_prefix_chunks": 1,
            },
            {
                "chunk_count": 3,
                "machine_artifact_prefix_chunks": 0,
            },
        ]
    )

    assert stats == {
        "chunk_count": 5,
        "machine_artifact_prefix_chunks": 1,
        "machine_artifact_prefix_ratio": 0.2,
    }


def test_page_quality_flags_detect_auth_like_and_zero_chunk_pages() -> None:
    auth_page = local_db_slice_eval.PageRow(
        id=13,
        source_id=None,
        uri="https://example.com/identity/account/login",
        title="Вход",
        content="Вход Регистрация",
    )
    normal_page = local_db_slice_eval.PageRow(
        id=14,
        source_id=None,
        uri="https://example.com/docs",
        title="Docs",
        content="Useful docs",
    )

    assert local_db_slice_eval.page_quality_flags(
        auth_page,
        [ChunkData(0, 0, 5, "Вход Регистрация", "text", token_count=2)],
    ) == {
        "auth_like_page": True,
        "auth_like_chunks": 1,
        "zero_chunk_nonempty_page": False,
    }
    assert local_db_slice_eval.page_quality_flags(normal_page, []) == {
        "auth_like_page": False,
        "auth_like_chunks": 0,
        "zero_chunk_nonempty_page": True,
    }


def test_aggregate_quality_stats_counts_page_flags() -> None:
    stats = local_db_slice_eval.aggregate_quality_stats(
        [
            {
                "auth_like_page": True,
                "auth_like_chunks": 2,
                "zero_chunk_nonempty_page": False,
            },
            {
                "auth_like_page": False,
                "auth_like_chunks": 0,
                "zero_chunk_nonempty_page": True,
            },
        ]
    )

    assert stats == {
        "auth_like_pages": 1,
        "auth_like_chunks": 2,
        "zero_chunk_nonempty_pages": 1,
    }


def test_summarize_page_materialize_mode_applies_metadata_policy() -> None:
    page = local_db_slice_eval.PageRow(
        id=11,
        source_id=None,
        uri="https://example.com/assets/vendor/lib/CHANGELOG/",
        title="CHANGELOG",
        content="# CHANGELOG\n\n" + ("Bug fixes\n" * 20),
        raw_content_type="text/html",
    )

    summary, chunks = local_db_slice_eval.summarize_page(page)

    assert len(chunks) == 1
    assert chunks[0].kind == "file_summary"
    assert summary["kind_counts"] == {"file_summary": 1}
    assert summary["mode"] == "materialize"


def test_summarize_page_materialize_mode_handles_oversize_page(monkeypatch) -> None:
    from jobs.crawler import tasks as crawler_tasks

    monkeypatch.setattr(crawler_tasks, "EMBEDDING_DOCUMENT_MAX_CHARS", 10)
    page = local_db_slice_eval.PageRow(
        id=12,
        source_id=None,
        uri="https://example.com/large",
        title="Large",
        content="x" * 11,
    )

    summary, chunks = local_db_slice_eval.summarize_page(page)

    assert chunks == []
    assert summary["chunk_count"] == 0
    assert summary["machine_artifact_prefix_chunks"] == 0
    assert summary["mode"] == "materialize"


def test_rank_page_chunks_returns_source_payload_shape() -> None:
    page = local_db_slice_eval.PageRow(
        id=20,
        source_id=None,
        uri="https://example.com/report",
        title="Report",
        content="",
    )
    chunks = [
        ChunkData(
            0,
            0,
            32,
            "Report says support@example.com is the support email.",
            "text",
            token_count=6,
        )
    ]

    ranked = local_db_slice_eval.rank_page_chunks(
        page,
        chunks,
        query="support email",
        deterministic_rerank=True,
        top_k=1,
    )

    assert ranked == [
        {
            "citation_id": 0,
            "page_id": 20,
            "uri": "https://example.com/report",
            "title": "Report",
            "kind": "text",
            "chunk_ix": 0,
            "rerank_score": ranked[0]["rerank_score"],
            "text_preview": "Report says support@example.com is the support email.",
        }
    ]


def test_rank_global_chunks_ranks_across_pages() -> None:
    pages = [
        local_db_slice_eval.PageRow(
            id=21,
            source_id=None,
            uri="https://example.com/teacher",
            title="Teacher page",
            content="",
        ),
        local_db_slice_eval.PageRow(
            id=22,
            source_id=None,
            uri="https://example.com/redirect",
            title="Redirect",
            content="",
        ),
    ]
    page_chunks = [
        (
            pages[0],
            [
                ChunkData(
                    0,
                    None,
                    None,
                    "Document indexed as metadata only. Title: Школа возможностей.",
                    "file_summary",
                    token_count=6,
                )
            ],
        ),
        (
            pages[1],
            [
                ChunkData(
                    0,
                    None,
                    None,
                    "Document indexed as metadata only. Title: 301 Moved Permanently.",
                    "file_summary",
                    token_count=7,
                )
            ],
        ),
    ]

    ranked = local_db_slice_eval.rank_global_chunks(
        page_chunks,
        query="Школа возможностей",
        deterministic_rerank=True,
        top_k=2,
    )

    assert ranked[0]["uri"] == "https://example.com/teacher"
    assert ranked[0]["kind"] == "file_summary"
    assert local_db_slice_eval.expected_uri_hit(
        ranked,
        "https://example.com/teacher",
    )


def test_context_summary_from_snippets_reports_citations() -> None:
    page = local_db_slice_eval.PageRow(
        id=23,
        source_id=None,
        uri="https://example.com/teacher",
        title="Teacher page",
        content="",
    )
    selected = local_db_slice_eval.select_global_snippets(
        [
            (
                page,
                [
                    ChunkData(
                        0,
                        None,
                        None,
                        "Document indexed as metadata only. Title: Школа возможностей.",
                        "file_summary",
                        token_count=6,
                    )
                ],
            )
        ],
        query="Школа возможностей",
        deterministic_rerank=True,
    )

    summary = local_db_slice_eval.context_summary_from_snippets(selected)

    assert summary == {
        "snippet_count": 1,
        "source_count": 1,
        "citation_ids": [0],
        "uris": ["https://example.com/teacher"],
        "kinds": ["file_summary"],
    }


def test_embed_and_rank_chunks_uses_cosine_similarity() -> None:
    chunks = [
        ChunkData(0, 0, 10, "alpha source", "text", token_count=2),
        ChunkData(1, 11, 20, "beta source", "text", token_count=2),
    ]

    def _embedder(texts: list[str]) -> list[list[float]]:
        assert texts == ["alpha query", "alpha source", "beta source"]
        return [
            [1.0, 0.0],
            [1.0, 0.0],
            [0.0, 1.0],
        ]

    ranked = local_db_slice_eval.embed_and_rank_chunks(
        chunks,
        query="alpha query",
        embedder=_embedder,
        top_k=2,
    )

    assert ranked[0]["chunk_ix"] == 0
    assert ranked[0]["score"] == 1.0
    assert ranked[1]["chunk_ix"] == 1
    assert ranked[1]["score"] == 0.0


def test_embed_and_rank_page_chunks_ranks_across_pages() -> None:
    pages = [
        local_db_slice_eval.PageRow(
            id=1,
            source_id=None,
            uri="https://example.com/a",
            title="A",
            content="",
        ),
        local_db_slice_eval.PageRow(
            id=2,
            source_id=None,
            uri="https://example.com/b",
            title="B",
            content="",
        ),
    ]
    page_chunks = [
        (pages[0], [ChunkData(0, 0, 5, "alpha", "text", token_count=1)]),
        (pages[1], [ChunkData(0, 0, 4, "beta", "file_summary", token_count=1)]),
    ]

    def _embedder(texts: list[str]) -> list[list[float]]:
        assert texts == ["beta query", "alpha", "beta"]
        return [
            [0.0, 1.0],
            [1.0, 0.0],
            [0.0, 1.0],
        ]

    ranked = local_db_slice_eval.embed_and_rank_page_chunks(
        page_chunks,
        query="beta query",
        embedder=_embedder,
        top_k=2,
    )

    assert ranked[0]["uri"] == "https://example.com/b"
    assert ranked[0]["kind"] == "file_summary"
    assert local_db_slice_eval.expected_uri_hit(
        ranked,
        "https://example.com/b",
    )
    assert not local_db_slice_eval.expected_uri_hit(
        ranked,
        "https://missing.example.com",
    )


def test_cosine_similarity_rejects_mismatched_dimensions() -> None:
    try:
        local_db_slice_eval.cosine_similarity([1.0], [1.0, 2.0])
    except ValueError:
        pass
    else:
        raise AssertionError("Expected mismatched vector dimensions to fail")


def test_parser_defaults_to_project_database_uri() -> None:
    parser = local_db_slice_eval.build_parser()
    args = parser.parse_args([])

    assert isinstance(args, argparse.Namespace)
    assert args.limit == 5
    assert args.page_id == []


def test_normalize_asyncpg_dsn_accepts_sqlalchemy_async_scheme() -> None:
    assert (
        local_db_slice_eval.normalize_asyncpg_dsn(
            "postgresql+asyncpg://user@localhost/db"
        )
        == "postgresql://user@localhost/db"
    )


def test_normalize_meta_accepts_json_string() -> None:
    assert local_db_slice_eval.normalize_meta('{"doc_type":"html"}') == {
        "doc_type": "html"
    }
