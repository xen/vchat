from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import asyncpg

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jobs.crawler import tasks as crawler_tasks
from jobs.embedder.chunking import ChunkData, chunk_document_text
from vchat.settings import cfg
from vchat.views.chat import ctx as ctx_mod


@dataclass(frozen=True)
class PageRow:
    id: int
    source_id: int | None
    uri: str | None
    title: str | None
    content: str | None
    content_hash: str | None = None
    meta: dict[str, Any] | None = None
    status_error: str | None = None
    raw_content_type: str | None = None
    raw_content_size: int | None = None


class DeterministicReranker:
    def predict(self, pairs, show_progress_bar=False):
        _ = show_progress_bar
        return [0.2 for _ in pairs]


class EvalProvider:
    def token_count(self, text: str, model=None) -> int:
        _ = model
        return len((text or "").split())


class EvalModel:
    id = "eval-model"


MACHINE_ARTIFACT_PREFIX_RE = re.compile(
    r"""(?ix)
    ^\s*
    (?:
        \{(?=[\s\S]{0,2000}$)(?=[\s\S]*"(?:cookieKey|isActive|__typename|props|pageProps|buildId|webpack)"\s*:)
        | \[(?:\s*\{|\s*\[)
        | (?:window|self)\.
        | (?:var|let|const)\s+[A-Za-z_$][\w$]*\s*=
        | (?:!function|function)\s*\(
    )
    """
)
AUTH_LIKE_URI_RE = re.compile(
    r"/(?:login|signin|sign-in|identity|account|auth|register|registration)(?:/|$)",
    re.I,
)
AUTH_LIKE_TITLE_RE = re.compile(
    r"^(?:вход|регистрация|login|sign in|sign-in|registration)$",
    re.I,
)


def looks_like_machine_artifact_prefix(text: str) -> bool:
    preview = (text or "").lstrip()[:2000]
    if not preview:
        return False
    return bool(MACHINE_ARTIFACT_PREFIX_RE.match(preview))


def chunk_noise_stats(chunks: list[ChunkData]) -> dict[str, Any]:
    artifact_count = sum(
        1 for chunk in chunks if looks_like_machine_artifact_prefix(chunk.text)
    )
    return {
        "machine_artifact_prefix_chunks": artifact_count,
        "machine_artifact_prefix_ratio": round(
            artifact_count / len(chunks),
            6,
        )
        if chunks
        else 0.0,
    }


def aggregate_noise_stats(page_results: list[dict[str, Any]]) -> dict[str, Any]:
    chunk_count = sum(int(page["chunk_count"]) for page in page_results)
    artifact_count = sum(
        int(page["machine_artifact_prefix_chunks"]) for page in page_results
    )
    return {
        "chunk_count": chunk_count,
        "machine_artifact_prefix_chunks": artifact_count,
        "machine_artifact_prefix_ratio": round(
            artifact_count / chunk_count,
            6,
        )
        if chunk_count
        else 0.0,
    }


def is_auth_like_page(page: PageRow) -> bool:
    uri = page.uri or ""
    title = (page.title or "").strip()
    return bool(AUTH_LIKE_URI_RE.search(uri) or AUTH_LIKE_TITLE_RE.match(title))


def page_quality_flags(page: PageRow, chunks: list[ChunkData]) -> dict[str, Any]:
    auth_like = is_auth_like_page(page)
    zero_chunk_nonempty = bool((page.content or "").strip()) and not chunks
    return {
        "auth_like_page": auth_like,
        "auth_like_chunks": len(chunks) if auth_like else 0,
        "zero_chunk_nonempty_page": zero_chunk_nonempty,
    }


def aggregate_quality_stats(page_results: list[dict[str, Any]]) -> dict[str, Any]:
    auth_like_pages = sum(1 for page in page_results if page["auth_like_page"])
    auth_like_chunks = sum(int(page["auth_like_chunks"]) for page in page_results)
    zero_chunk_nonempty_pages = sum(
        1 for page in page_results if page["zero_chunk_nonempty_page"]
    )
    return {
        "auth_like_pages": auth_like_pages,
        "auth_like_chunks": auth_like_chunks,
        "zero_chunk_nonempty_pages": zero_chunk_nonempty_pages,
    }


def build_query(
    *,
    page_ids: list[int],
    uri_like: str | None,
    limit: int,
) -> tuple[str, list[Any]]:
    where = ["content is not null", "length(content) > 0"]
    args: list[Any] = []
    if page_ids:
        args.append(page_ids)
        where.append(f"id = any(${len(args)}::int[])")
    if uri_like:
        args.append(uri_like)
        where.append(f"uri ilike ${len(args)}")
    sql = f"""
        select id, uri, title, content
        , source_id, hash, meta, status_error, raw_content_type, raw_content_size
        from page
        where {" and ".join(where)}
        order by id
        limit {int(limit)}
    """
    return sql, args


async def fetch_pages(
    *,
    database_uri: str,
    page_ids: list[int],
    uri_like: str | None,
    limit: int,
) -> list[PageRow]:
    sql, args = build_query(page_ids=page_ids, uri_like=uri_like, limit=limit)
    conn = await asyncpg.connect(normalize_asyncpg_dsn(database_uri))
    try:
        rows = await conn.fetch(sql, *args)
    finally:
        await conn.close()
    return [
        PageRow(
            id=row["id"],
            source_id=row["source_id"],
            uri=row["uri"],
            title=row["title"],
            content=row["content"],
            content_hash=row["hash"],
            meta=row["meta"],
            status_error=row["status_error"],
            raw_content_type=row["raw_content_type"],
            raw_content_size=row["raw_content_size"],
        )
        for row in rows
    ]


def normalize_asyncpg_dsn(database_uri: str) -> str:
    return database_uri.replace("postgresql+asyncpg://", "postgresql://", 1)


def normalize_meta(meta: Any) -> dict[str, Any]:
    if meta is None:
        return {}
    if isinstance(meta, dict):
        return dict(meta)
    if isinstance(meta, str):
        payload = meta.strip()
        if not payload:
            return {}
        loaded = json.loads(payload)
        if not isinstance(loaded, dict):
            raise ValueError("Page meta JSON must be an object")
        return loaded
    return dict(meta)


class _FakeExecuteResult:
    def scalar_one(self) -> int:
        return 0

    def scalars(self) -> "_FakeExecuteResult":
        return self

    def all(self) -> list[Any]:
        return []


class _FakeMaterializeSession:
    def __init__(self, page: Any) -> None:
        self.page = page
        self.added: list[Any] = []

    def get(self, model, page_id):
        _ = model
        return self.page if page_id == self.page.id else None

    def execute(self, stmt, params=None):
        _ = stmt, params
        return _FakeExecuteResult()

    def add(self, obj) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def expunge_all(self) -> None:
        pass


def materialize_page_in_memory(page: PageRow) -> list[ChunkData]:
    doc = SimpleNamespace(
        id=page.id,
        source_id=page.source_id,
        uri=page.uri,
        title=page.title,
        content=page.content or "",
        content_hash=page.content_hash or "",
        hash_value=page.content_hash or "",
        meta=normalize_meta(page.meta),
        status_error=page.status_error,
        raw_content_type=page.raw_content_type,
        raw_content_size=page.raw_content_size,
    )

    def patch_meta(*, remove=(), **updates) -> None:
        meta = dict(doc.meta or {})
        for key in remove:
            meta.pop(key, None)
        meta.update(updates)
        doc.meta = meta

    doc.patch_meta = patch_meta
    session = _FakeMaterializeSession(doc)
    crawler_tasks.materialize_page_chunks(session, doc)
    return [
        ChunkData(
            index=chunk.chunk_ix,
            start=chunk.start_offset,
            end=chunk.end_offset,
            text=chunk.text,
            kind=chunk.kind,
            header_text=chunk.header_text,
            section_path=chunk.section_path,
            entity_terms=chunk.entity_terms,
            token_count=chunk.token_count,
        )
        for chunk in session.added
    ]


def summarize_page(
    page: PageRow,
    *,
    mode: str = "materialize",
    chunker: Callable[[str], list[ChunkData]] = chunk_document_text,
) -> tuple[dict[str, Any], list[ChunkData]]:
    if mode == "materialize":
        chunks = materialize_page_in_memory(page)
    elif mode == "chunker":
        chunks = chunker(page.content or "")
    else:
        raise ValueError(f"Unknown eval mode: {mode}")

    kind_counts = Counter(chunk.kind for chunk in chunks)
    chunk_chars = [len(chunk.text or "") for chunk in chunks]
    summary = {
        "page_id": page.id,
        "uri": page.uri,
        "title": page.title,
        "content_chars": len(page.content or ""),
        "chunk_count": len(chunks),
        "chunk_chars": sum(chunk_chars),
        "max_chunk_chars": max(chunk_chars, default=0),
        "kind_counts": dict(sorted(kind_counts.items())),
        "mode": mode,
        **chunk_noise_stats(chunks),
        **page_quality_flags(page, chunks),
    }
    return summary, chunks


def chunks_to_snippets(page: PageRow, chunks: list[ChunkData]) -> list[ctx_mod.Snippet]:
    return [
        ctx_mod.Snippet(
            id=chunk.index,
            text=chunk.text,
            document_id=page.id,
            chunk_ix=chunk.index,
            uri=page.uri,
            title=page.title,
            kind=chunk.kind,
            header_text=chunk.header_text,
            section_path=chunk.section_path,
            entity_terms=chunk.entity_terms,
            src="local_db_slice",
        )
        for chunk in chunks
    ]


def _selected_snippets_to_rows(snippets: list[ctx_mod.Snippet]) -> list[dict[str, Any]]:
    return [
        {
            "citation_id": idx,
            "page_id": snippet.document_id,
            "uri": snippet.uri,
            "title": snippet.title,
            "kind": snippet.kind,
            "chunk_ix": snippet.chunk_ix,
            "rerank_score": snippet.rerank_score,
            "text_preview": (snippet.text or "")[:240],
        }
        for idx, snippet in enumerate(snippets)
    ]


def rank_page_chunks(
    page: PageRow,
    chunks: list[ChunkData],
    *,
    query: str,
    deterministic_rerank: bool,
    top_k: int,
) -> list[dict[str, Any]]:
    if deterministic_rerank:
        ctx_mod._rerank_model = DeterministicReranker()
    ranked = ctx_mod.crossrerank(query, chunks_to_snippets(page, chunks))
    filtered = ctx_mod.filter_snippets_by_document_relevance(ranked)
    selected = ctx_mod.select_context_snippets(
        filtered,
        provider=EvalProvider(),
        model=EvalModel(),
    )
    return _selected_snippets_to_rows(selected[:top_k])


def select_global_snippets(
    page_chunks: list[tuple[PageRow, list[ChunkData]]],
    *,
    query: str,
    deterministic_rerank: bool,
) -> list[ctx_mod.Snippet]:
    if deterministic_rerank:
        ctx_mod._rerank_model = DeterministicReranker()
    snippets = [
        snippet
        for page, chunks in page_chunks
        for snippet in chunks_to_snippets(page, chunks)
    ]
    ranked = ctx_mod.crossrerank(query, snippets)
    filtered = ctx_mod.filter_snippets_by_document_relevance(ranked)
    return ctx_mod.select_context_snippets(
        filtered,
        provider=EvalProvider(),
        model=EvalModel(),
    )


def rank_global_chunks(
    page_chunks: list[tuple[PageRow, list[ChunkData]]],
    *,
    query: str,
    deterministic_rerank: bool,
    top_k: int,
) -> list[dict[str, Any]]:
    selected = select_global_snippets(
        page_chunks,
        query=query,
        deterministic_rerank=deterministic_rerank,
    )
    return _selected_snippets_to_rows(selected[:top_k])


def context_summary_from_snippets(snippets: list[ctx_mod.Snippet]) -> dict[str, Any]:
    message = ctx_mod.build_context_from_snippets(
        snippets,
        provider=EvalProvider(),
        model=EvalModel(),
    )
    payload = json.loads(message.content.split("\n", 1)[1])
    payload_snippets = payload["snippets"]
    return {
        "snippet_count": len(payload_snippets),
        "source_count": len(
            {snippet.get("uri") for snippet in payload_snippets if snippet.get("uri")}
        ),
        "citation_ids": [snippet["citation_id"] for snippet in payload_snippets],
        "uris": [snippet.get("uri") for snippet in payload_snippets],
        "kinds": [snippet.get("kind") for snippet in payload_snippets],
    }


def cosine_similarity(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _as_float_vector(value: Any) -> list[float]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    return [float(item) for item in value]


def embed_texts(model: Any, texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    encoded = model.encode(texts, normalize_embeddings=True)
    return [_as_float_vector(item) for item in encoded]


def load_eval_embedding_model() -> Any:
    from jobs.embedder.model import load_embedding_model

    return load_embedding_model()


def embed_and_rank_chunks(
    chunks: list[ChunkData],
    *,
    query: str,
    embedder: Callable[[list[str]], list[list[float]]],
    top_k: int,
) -> list[dict[str, Any]]:
    texts = [chunk.text for chunk in chunks]
    vectors = embedder([query, *texts])
    if len(vectors) != len(texts) + 1:
        raise ValueError("Embedder returned an unexpected vector count")
    query_vector = vectors[0]
    ranked = sorted(
        (
            (
                cosine_similarity(query_vector, vector),
                chunk,
            )
            for chunk, vector in zip(chunks, vectors[1:], strict=True)
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    return [
        {
            "rank": rank,
            "score": round(score, 6),
            "kind": chunk.kind,
            "chunk_ix": chunk.index,
            "text_preview": (chunk.text or "")[:240],
        }
        for rank, (score, chunk) in enumerate(ranked[:top_k], start=1)
    ]


def embed_and_rank_page_chunks(
    page_chunks: list[tuple[PageRow, list[ChunkData]]],
    *,
    query: str,
    embedder: Callable[[list[str]], list[list[float]]],
    top_k: int,
) -> list[dict[str, Any]]:
    items: list[tuple[PageRow, ChunkData]] = [
        (page, chunk)
        for page, chunks in page_chunks
        for chunk in chunks
    ]
    texts = [chunk.text for _, chunk in items]
    vectors = embedder([query, *texts])
    if len(vectors) != len(texts) + 1:
        raise ValueError("Embedder returned an unexpected vector count")
    query_vector = vectors[0]
    ranked = sorted(
        (
            (
                cosine_similarity(query_vector, vector),
                page,
                chunk,
            )
            for (page, chunk), vector in zip(items, vectors[1:], strict=True)
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    return [
        {
            "rank": rank,
            "score": round(score, 6),
            "page_id": page.id,
            "uri": page.uri,
            "title": page.title,
            "kind": chunk.kind,
            "chunk_ix": chunk.index,
            "text_preview": (chunk.text or "")[:240],
        }
        for rank, (score, page, chunk) in enumerate(ranked[:top_k], start=1)
    ]


def expected_uri_hit(
    ranked: list[dict[str, Any]],
    expected_uri: str | None,
) -> bool | None:
    if not expected_uri:
        return None
    return any(item.get("uri") == expected_uri for item in ranked)


async def run_eval(args: argparse.Namespace) -> dict[str, Any]:
    pages = await fetch_pages(
        database_uri=args.database_uri,
        page_ids=args.page_id,
        uri_like=args.uri_like,
        limit=args.limit,
    )
    page_results: list[dict[str, Any]] = []
    embedding_model = load_eval_embedding_model() if args.embed_query else None
    materialized: list[tuple[PageRow, list[ChunkData]]] = []
    for page in pages:
        summary, chunks = summarize_page(page, mode=args.mode)
        materialized.append((page, chunks))
        if args.query:
            summary["top_chunks"] = rank_page_chunks(
                page,
                chunks,
                query=args.query,
                deterministic_rerank=args.deterministic_rerank,
                top_k=args.top_k,
            )
        if args.embed_query:
            summary["embedding_top_chunks"] = embed_and_rank_chunks(
                chunks,
                query=args.embed_query,
                embedder=lambda texts: embed_texts(embedding_model, texts),
                top_k=args.top_k,
            )
        page_results.append(summary)
    query_global_top_chunks: list[dict[str, Any]] | None = None
    query_expected_hit: bool | None = None
    query_context: dict[str, Any] | None = None
    if args.query:
        selected_global_snippets = select_global_snippets(
            materialized,
            query=args.query,
            deterministic_rerank=args.deterministic_rerank,
        )
        query_global_top_chunks = _selected_snippets_to_rows(
            selected_global_snippets[: args.top_k]
        )
        query_context = context_summary_from_snippets(selected_global_snippets)
        query_expected_hit = expected_uri_hit(
            query_global_top_chunks,
            args.expected_uri,
        )
    embedding_global_top_chunks: list[dict[str, Any]] | None = None
    expected_hit: bool | None = None
    if args.embed_query:
        embedding_global_top_chunks = embed_and_rank_page_chunks(
            materialized,
            query=args.embed_query,
            embedder=lambda texts: embed_texts(embedding_model, texts),
            top_k=args.top_k,
        )
        expected_hit = expected_uri_hit(
            embedding_global_top_chunks,
            args.expected_uri,
        )
    return {
        "database_uri": args.database_uri,
        "page_count": len(page_results),
        "query": args.query,
        "embed_query": args.embed_query,
        "expected_uri": args.expected_uri,
        "expected_uri_hit": expected_hit,
        "query_expected_uri_hit": query_expected_hit,
        "noise": aggregate_noise_stats(page_results),
        "quality": aggregate_quality_stats(page_results),
        "query_global_top_chunks": query_global_top_chunks,
        "query_context": query_context,
        "embedding_global_top_chunks": embedding_global_top_chunks,
        "pages": page_results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a read-only local DB RAG chunk/retrieval slice eval."
    )
    parser.add_argument(
        "--database-uri",
        default=cfg.database_uri,
        help="PostgreSQL URI. Defaults to project config.",
    )
    parser.add_argument(
        "--page-id",
        action="append",
        type=int,
        default=[],
        help="Page id to include. Can be repeated.",
    )
    parser.add_argument("--uri-like", help="SQL ILIKE pattern for page.uri.")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--mode",
        choices=("materialize", "chunker"),
        default="materialize",
        help="Use full materialization policy or raw chunker only.",
    )
    parser.add_argument("--query", help="Optional query to rank generated chunks.")
    parser.add_argument(
        "--embed-query",
        help=(
            "Optional query for in-memory embedding similarity over generated chunks. "
            "Loads the local embedding model and does not write vectors to DB."
        ),
    )
    parser.add_argument(
        "--expected-uri",
        help="Expected source URI for top-level embedding hit@k reporting.",
    )
    parser.add_argument(
        "--deterministic-rerank",
        action="store_true",
        help="Use a fixed reranker score for reproducible local scoring.",
    )
    parser.add_argument("--top-k", type=int, default=5)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = asyncio.run(run_eval(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
