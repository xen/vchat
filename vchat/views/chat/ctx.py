from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import sqlalchemy as sa
import tiktoken
from pgvector.sqlalchemy import Vector
from sqlalchemy.ext.asyncio import AsyncSession

from vchat.ai_providers import BaseAIProvider, ModelInfo
from vchat.embeddings import load_embedding_model
from vchat.models import ChatMsg
from vchat.settings import config

from . import retrieval_config as rcnf
from ._types import Msg

logger = logging.getLogger(__name__)
cfg = config

EMBEDDING_QUERY_PROMPT = (
    "Instruct: Дан вопрос, необходимо найти абзац текста с ответом\nQuery: "
)
MAX_SNIPPET_LEN = 2600
MAX_CONTEXT_SNIPPETS = 6
MAX_SNIPPETS_PER_SOURCE = 2
TAIL_MSG_LIMIT = 5
VECTOR_TOP_K = 10
FT_TOP_M = 10
MAX_LEXICAL_TERMS = 8
VECTOR_MAX_DIST = 0.68
MAX_CONTEXT_SNIPPET_TOKENS = 5000
MAX_INPUT_CONTEXT_TOKENS = 10000
CONTEXT_SAFETY_MARGIN = 1024
RERANK_LIMIT = 48
RRF_K = 60
RERANK_MODEL = cfg.get(
    "reranker_model_id", "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
)

SNIPPET_INJECTION_PATTERNS = re.compile(
    r"(?i)\b(system|assistant|user|instruction|command|rules?|prompt)\b"
)

user_id_ctx: contextvars.ContextVar[int | str | None] = contextvars.ContextVar(
    "user_id", default=None
)
chat_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "chat_id", default=None
)

lang_models = {"en": "en_core_web_sm", "ru": "ru_core_news_sm"}
_nlps: dict[str, Any] = {}
_embed_model: Any | None = None
_rerank_model: Any | None = None
nlps = _nlps


@dataclass(frozen=True)
class ContextSnippet:
    text: str
    kind: str | None = None
    src: str | None = None
    uri: str | None = None
    title: str | None = None
    display_path: str | None = None
    header_text: str | None = None
    section_path: str | None = None
    entity_terms: list[str] | None = None


@dataclass(frozen=True)
class ContextPayload:
    type: str = "rag_context"
    version: int = 1
    snippets: list[ContextSnippet] = field(default_factory=list)


@dataclass(frozen=True)
class ContextPolicy:
    quote_mode: bool
    table_mode: bool
    enumeration_mode: bool
    has_source_url: bool
    has_quote_candidate: bool
    has_table_candidate: bool
    has_enumeration_coverage: bool
    reason_code: str


@dataclass(frozen=True)
class Snippet:
    id: int | None = None
    text: str | None = None
    chat_id: str | None = None
    document_id: int | None = None
    chunk_ix: int | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    dist: float | None = None
    src: str | None = None
    uri: str | None = None
    title: str | None = None
    kind: str | None = None
    header_text: str | None = None
    section_path: str | None = None
    entity_terms: list[str] | None = None
    token_count: int | None = None
    rerank_score: float | None = None
    retrieval_origins: list[str] | None = None

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> "Snippet":
        return cls(
            id=row.get("id"),
            text=row.get("text"),
            chat_id=row.get("chat_id"),
            document_id=row.get("document_id"),
            chunk_ix=row.get("chunk_ix"),
            start_offset=row.get("start_offset"),
            end_offset=row.get("end_offset"),
            dist=row.get("dist"),
            src=row.get("src"),
            uri=row.get("uri"),
            title=row.get("title"),
            kind=row.get("kind"),
            header_text=row.get("header_text"),
            section_path=row.get("section_path"),
            entity_terms=row.get("entity_terms"),
            token_count=row.get("token_count"),
            rerank_score=row.get("rerank_score"),
        )

    @property
    def display_path(self) -> str | None:
        title = (self.title or "").strip()
        branch = (self.section_path or self.header_text or "").strip()
        if title and branch and branch != title:
            return f"{title} / {branch}"
        return title or branch or None


@dataclass(frozen=True)
class ContextResult:
    messages: list[Msg]
    used_chunks: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    policy: dict[str, Any]
    coverage: dict[str, Any]


def get_cld2():
    try:
        import pycld2 as cld2  # type: ignore[import]
    except ImportError:
        return None
    return cld2


cld2: Any | None = get_cld2()


def load_nlp(lang: str):
    if lang not in lang_models:
        return None
    if lang in _nlps:
        return _nlps[lang]
    try:
        import spacy  # type: ignore[import]

        nlp = spacy.load(lang_models[lang])
        _nlps[lang] = nlp
        return nlp
    except Exception:
        logger.exception("Failed to load spaCy model for %s", lang)
        return None


def detect_lang(text: str) -> str | None:
    detector = cld2
    if not detector:
        return None
    try:
        is_reliable, _, details = detector.detect(text)
    except Exception:
        return None
    if is_reliable:
        lang = details[0][1]
        if lang in lang_models:
            return lang
    return None


def lexical_query(text: str) -> str:
    lang = detect_lang(text) or "en"
    nlp = load_nlp(lang)
    if not nlp:
        return text

    doc = nlp(text)
    scores: dict[str, float] = {}
    quoted = re.findall(r'"([^"]+)"', text)
    for item in quoted:
        phrase = item.strip()
        if phrase:
            scores[phrase] = scores.get(phrase, 0.0) + 10.0

    for ent in doc.ents:
        phrase = ent.text.strip()
        if phrase:
            scores[phrase] = scores.get(phrase, 0.0) + 8.0

    for token in doc:
        if token.is_stop or token.is_punct or token.is_space:
            continue
        if token.pos_ not in {"NOUN", "PROPN", "ADJ", "NUM"}:
            continue

        lemma = token.lemma_.strip()
        if len(lemma) >= 2:
            scores[lemma] = scores.get(lemma, 0.0) + 2.0

        if token.pos_ not in {"NOUN", "PROPN"}:
            continue

        left = [
            child.lemma_.strip()
            for child in token.lefts
            if child.dep_ in {"amod", "compound", "flat", "nummod"}
        ]
        right = [
            child.lemma_.strip()
            for child in token.rights
            if child.dep_ in {"nmod", "flat", "compound"}
        ]
        phrase = " ".join(part for part in [*left, lemma, *right] if part).strip()
        if len(phrase) >= 3 and phrase != lemma:
            scores[phrase] = scores.get(phrase, 0.0) + 5.0

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    terms = [item[0] for item in ranked[:MAX_LEXICAL_TERMS]]
    if not terms:
        return text
    return " OR ".join(terms)


def queryprofile(text: str) -> dict[str, Any]:
    rewritten = lexical_query(text)
    lowered = text.lower()
    lexical_terms: list[str] = []
    seen_terms = set()
    for term in re.split(r"\s+OR\s+", rewritten):
        normalized = term.strip().strip('"').lower()
        if len(normalized) < 2 or normalized in seen_terms:
            continue
        seen_terms.add(normalized)
        lexical_terms.append(normalized)

    table_mode = any(
        token in lowered
        for token in (
            "table",
            "таблиц",
            "column",
            "колонк",
            "header",
            "строк",
            "row",
        )
    )
    quote_mode = any(
        token in lowered
        for token in ("quote", "citation", "source", "цитат", "дослов", "источник")
    )
    enumeration_mode = any(
        token in lowered
        for token in (
            "all",
            "list",
            "enumerate",
            "перечис",
            "спис",
            "какие",
            "все",
        )
    )
    return {
        "lexical_query": rewritten,
        "lexical_terms": lexical_terms,
        "table_mode": table_mode,
        "quote_mode": quote_mode,
        "enumeration_mode": enumeration_mode,
    }


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        _embed_model = load_embedding_model()
    return _embed_model


def _truncate_embedding_payload(text: str, model: Any) -> str:
    payload = text or ""
    max_seq_length = int(
        cfg.get("embedding_max_seq_length") or getattr(model, "max_seq_length", 0) or 0
    )
    tokenizer = getattr(model, "tokenizer", None)
    if not payload or max_seq_length <= 0 or tokenizer is None:
        return payload

    try:
        encoded = tokenizer(
            payload,
            add_special_tokens=False,
            truncation=True,
            max_length=max_seq_length,
        )
    except TypeError:
        return payload
    except Exception:
        logger.exception("Failed to truncate embedding payload")
        return payload

    input_ids = None
    if isinstance(encoded, dict):
        input_ids = encoded.get("input_ids")
    else:
        input_ids = getattr(encoded, "input_ids", None)

    if not input_ids:
        return payload
    if input_ids and isinstance(input_ids[0], list):
        input_ids = input_ids[0]

    try:
        truncated = tokenizer.decode(
            input_ids,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )
    except TypeError:
        try:
            truncated = tokenizer.decode(input_ids, skip_special_tokens=True)
        except Exception:
            return payload
    except Exception:
        return payload

    return (truncated or payload).strip()


def embed_query(text: str) -> list[float]:
    model = _get_embed_model()
    payload = _truncate_embedding_payload(f"{EMBEDDING_QUERY_PROMPT}{text}", model)
    try:
        emb = model.encode([payload], normalize_embeddings=True, batch_size=1)
    except TypeError:
        emb = model.encode([payload], normalize_embeddings=True)
    return emb[0].tolist()


def token_count(text: str, model: str = "gpt-4o-mini") -> int:
    encoding = tiktoken.encoding_for_model(model)
    return len(encoding.encode(text or ""))


def text_summarizer(text: str, ratio: float, lang: str | None = None) -> str:
    source = (text or "").strip()
    if not source:
        return ""
    target = max(1, int(len(source) * max(0.0, min(1.0, ratio))))

    nlp = nlps.get(lang or "")
    if callable(nlp):
        try:
            doc = nlp(source)
            sentences = [
                getattr(sent, "text", "").strip() for sent in getattr(doc, "sents", [])
            ]
            summary = " ".join(part for part in sentences if part).strip()
            if summary:
                source = summary
        except Exception:  # nosec B110
            pass

    trimmed = source[: min(len(source), max(1, target))].strip()
    if len(source) > len(trimmed):
        return (trimmed[:200].rstrip() + " …") if trimmed else "…"
    return trimmed


def _vec_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{float(v):.6f}" for v in values) + "]"


async def _fetch_tail_messages(
    db: AsyncSession, chat_id: str, limit: int = TAIL_MSG_LIMIT
) -> list[Msg]:
    return await tail_messages(db=db, chat_id=chat_id, limit=limit)


async def _fetch_vector_chunks(
    db: AsyncSession,
    chat_id: str,
    query_vec: list[float],
    top_k: int = VECTOR_TOP_K,
) -> list[dict[str, Any]]:
    rows = await vector_supply(db=db, chat_id=chat_id, query_vec=query_vec, top_k=top_k)
    return [
        {
            "id": item.id,
            "content": item.text,
            "chat_id": item.chat_id,
            "document_id": item.document_id,
            "chunk_ix": item.chunk_ix,
            "dist": item.dist,
            "src": item.src,
            "uri": item.uri,
            "title": item.title,
            "kind": item.kind,
            "header_text": item.header_text,
            "section_path": item.section_path,
        }
        for item in rows
    ]


async def _fetch_ft_chunks(
    db: AsyncSession,
    query: str,
    top_m: int = FT_TOP_M,
) -> list[dict[str, Any]]:
    rows = await fulltext_supply(db=db, prompt_text=query, top_m=top_m)
    return [
        {
            "id": item.id,
            "content": item.text,
            "chat_id": item.chat_id,
            "document_id": item.document_id,
            "chunk_ix": item.chunk_ix,
            "dist": item.dist,
            "src": item.src,
            "uri": item.uri,
            "title": item.title,
            "kind": item.kind,
            "header_text": item.header_text,
            "section_path": item.section_path,
        }
        for item in rows
    ]


async def _fetch_user_memory_chunks(
    db: AsyncSession,
    *,
    user_id: int | str,
    chat_id: str,
    qvec: list[float],
    k_mem: int,
    tau: float,
    tau_fallback: float | None = None,
) -> list[dict[str, Any]]:
    if not user_id or not chat_id or not qvec:
        return []
    rows = (await db.execute(sa.text("SELECT 1"))).mappings().all()
    filtered = [row for row in rows if float(row.get("dist", 1.0)) <= tau]
    if not filtered and tau_fallback is not None:
        filtered = [row for row in rows if float(row.get("dist", 1.0)) <= tau_fallback]

    filtered.sort(key=lambda row: float(row.get("dist", 1.0)))
    filtered = filtered[: max(0, k_mem)]

    best_by_chat: dict[str, dict[str, Any]] = {}
    for row in filtered:
        key = str(row.get("chat_id") or "")
        if not key or key in best_by_chat:
            continue
        best_by_chat[key] = dict(row)
        if len(best_by_chat) >= max(0, k_mem):
            break
    return list(best_by_chat.values())


def _sanitize_snippet_text(text: str) -> str:
    return sanitize_snippet_text(text)


def _dedup_snippets(
    snippets: list[dict[str, Any]], max_prefix: int = 150
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen = set()
    for item in snippets:
        content = (item.get("content") or "").strip()
        if not content:
            continue
        key = content[:max_prefix].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _build_context_from_snippets(snippets: list[dict[str, Any]]) -> Msg:
    if not snippets:
        return Msg(role="developer", content="[context]\nнет релевантных фрагментов")

    lines = ["[context]"]
    for idx, item in enumerate(snippets):
        content = _sanitize_snippet_text(str(item.get("content") or ""))
        if not content:
            continue
        lines.append(f"[[citation:{idx}]] {content}")
    return Msg(role="developer", content="\n".join(lines))


def _dedup_by_text(messages: list[Msg]) -> list[Msg]:
    out: list[Msg] = []
    seen = set()
    for msg in messages:
        key = (msg.content or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(msg)
    return out


def _build_context_message(
    tail_messages_list: list[Msg],
    memory_messages: list[Msg],
    snippet_messages: list[Msg],
) -> list[Msg]:
    merged = _dedup_by_text([*tail_messages_list, *memory_messages, *snippet_messages])
    merged.append(Msg(role="system", content="[context]"))
    return merged


def loadrerank():
    global _rerank_model
    if _rerank_model is not None:
        return _rerank_model
    try:
        import sys

        import torch
        from sentence_transformers import CrossEncoder  # type: ignore[import]

        forced = cfg.get("reranker_device", "").strip().lower()
        if forced:
            device = forced
        elif sys.platform == "darwin" and torch.backends.mps.is_available():
            device = "mps"
        elif sys.platform.startswith("linux") and torch.cuda.is_available():
            device = "cuda"
        else:
            device = "cpu"

        _rerank_model = CrossEncoder(
            RERANK_MODEL,
            device=device,
            max_length=512,
            local_files_only=True,
        )
        logger.info("Rerank model loaded on device=%s", device)
    except Exception:
        logger.exception("Cross-encoder reranker is unavailable")
        _rerank_model = False
    return _rerank_model


async def tail_messages(
    db: AsyncSession,
    chat_id: str,
    limit: int = TAIL_MSG_LIMIT,
) -> list[Msg]:
    result = await db.execute(
        sa.select(ChatMsg.text, ChatMsg.role)
        .where(ChatMsg.chat_id == chat_id)
        .order_by(ChatMsg.created_at.desc())
        .limit(limit)
    )
    rows = result.all()
    return [Msg(role=role, content=text) for text, role in reversed(rows)]


async def vector_supply(
    db: AsyncSession,
    chat_id: str,
    query_vec: list[float],
    *,
    top_k: int = VECTOR_TOP_K,
) -> list[Snippet]:
    if top_k <= 0:
        return []

    k_chat = max(2, top_k // 2)
    k_kb = max(2, top_k - k_chat)

    chat_sql = sa.text(
        """
        SELECT c.id, c.text, c.chat_id, c.document_id, c.chunk_ix, c.start_offset, c.end_offset,
               c.kind, c.header_text, c.section_path, c.entity_terms, c.token_count,
               c.embedding <=> :qvec AS dist,
               'chat' AS src,
               NULL AS uri,
               NULL AS title
        FROM chunk c
        WHERE c.chat_id = :chat_id
          AND c.embedding IS NOT NULL
          AND c.embedding <=> :qvec <= :max_dist
        ORDER BY c.embedding <=> :qvec
        LIMIT :k_chat
        """
    ).bindparams(sa.bindparam("qvec", type_=Vector(cfg["vec_dim"])))

    kb_sql = sa.text(
        """
        SELECT c.id, c.text, c.chat_id, c.document_id, c.chunk_ix, c.start_offset, c.end_offset,
               c.kind, c.header_text, c.section_path, c.entity_terms, c.token_count,
               c.embedding <=> :qvec AS dist,
               'kb' AS src,
               d.uri AS uri,
               d.title AS title
        FROM chunk c
        JOIN document d ON c.document_id = d.id
        WHERE c.chat_id IS NULL
          AND c.document_id IS NOT NULL
          AND c.embedding IS NOT NULL
          AND c.embedding <=> :qvec <= :max_dist
        ORDER BY c.embedding <=> :qvec
        LIMIT :k_kb
        """
    ).bindparams(sa.bindparam("qvec", type_=Vector(cfg["vec_dim"])))

    params = {
        "qvec": query_vec,
        "chat_id": chat_id,
        "k_chat": k_chat,
        "k_kb": k_kb,
        "max_dist": VECTOR_MAX_DIST,
    }
    chat_rows = (await db.execute(chat_sql, params)).mappings().all()
    kb_rows = (await db.execute(kb_sql, params)).mappings().all()
    rows = [Snippet.from_mapping(dict(row)) for row in [*chat_rows, *kb_rows]]
    rows.sort(key=lambda item: item.dist if item.dist is not None else float("inf"))
    return rows[:top_k]


async def fulltext_supply(
    db: AsyncSession,
    prompt_text: str,
    *,
    top_m: int = FT_TOP_M,
) -> list[Snippet]:
    if not prompt_text or top_m <= 0:
        return []

    profile = queryprofile(prompt_text)
    query_text = profile["lexical_query"]
    logger.info("FTS query rewrite: %r -> %r", prompt_text, query_text)

    sql = sa.text(
        """
        SELECT c.id, c.text, c.chat_id, c.document_id, c.chunk_ix, c.start_offset, c.end_offset,
               c.kind, c.header_text, c.section_path, c.entity_terms, c.token_count,
               NULL::float8 AS dist,
               'ft' AS src,
               d.uri AS uri,
               d.title AS title
        FROM chunk c
        LEFT JOIN document d ON c.document_id = d.id
        WHERE (
               c.fts @@ websearch_to_tsquery('russian', :q)
            OR c.fts @@ websearch_to_tsquery('english', :q)
        )
        ORDER BY
            CASE
                WHEN :table_mode = true AND c.kind IN ('table', 'table_rows') THEN 3
                WHEN c.kind IN ('section_summary', 'summary') THEN 1
                ELSE 0
            END DESC,
            (
                ts_rank_cd(c.fts, websearch_to_tsquery('russian', :q))
                + ts_rank_cd(c.fts, websearch_to_tsquery('english', :q))
            ) DESC,
            c.id DESC
        LIMIT :m
        """
    )
    rows = (
        (
            await db.execute(
                sql, {"q": query_text, "m": top_m, "table_mode": profile["table_mode"]}
            )
        )
        .mappings()
        .all()
    )
    return [Snippet.from_mapping(dict(row)) for row in rows]


def reciprocal_rank_fusion(rankings: list[list[Snippet]]) -> list[Snippet]:
    fused: dict[tuple[Any, ...], Snippet] = {}
    scores: dict[tuple[Any, ...], float] = {}
    origins: dict[tuple[Any, ...], list[str]] = {}

    for ranking in rankings:
        for rank, snippet in enumerate(ranking, start=1):
            text = (snippet.text or "").strip()
            if not text:
                continue
            key = (
                snippet.document_id,
                snippet.chat_id,
                snippet.id,
                snippet.chunk_ix,
                snippet.start_offset,
                snippet.end_offset,
                text[:200],
            )
            if key not in fused:
                fused[key] = snippet
                origins[key] = []
            bucket = origins[key]
            if snippet.src and snippet.src not in bucket:
                bucket.append(snippet.src)
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)

    ranked: list[Snippet] = []
    for key, snippet in fused.items():
        ranked.append(
            Snippet(
                **{
                    **asdict(snippet),
                    "rerank_score": round(scores[key], 6),
                    "retrieval_origins": origins[key] or None,
                }
            )
        )
    ranked.sort(key=lambda item: item.rerank_score or 0.0, reverse=True)
    return ranked


def sanitize_snippet_text(text: str) -> str:
    clean = (text or "").replace("\r", " ").replace("\n", " ").strip()
    clean = re.sub(r"\s+", " ", clean)
    clean = clean.replace("```", '"')
    clean_lower = clean.lower()
    for prefix in ("system:", "assistant:", "user:"):
        if clean_lower.startswith(prefix):
            clean = clean[len(prefix) :].strip()
            clean_lower = clean.lower()
    clean = SNIPPET_INJECTION_PATTERNS.sub("[redacted]", clean)
    return clean[:MAX_SNIPPET_LEN].strip()


def crossrerank(query: str, snippets: list[Snippet]) -> list[Snippet]:
    ranked = [snippet for snippet in snippets if (snippet.text or "").strip()]
    if not ranked:
        return []

    model = loadrerank()
    if not model:
        return ranked[:RERANK_LIMIT]

    profile = queryprofile(query)
    pairs = []
    for snippet in ranked[:RERANK_LIMIT]:
        snippet_text = snippet.text or ""
        if snippet.kind in {"table", "table_rows"}:
            snippet_text = re.sub(
                r"\n{3,}", "\n\n", snippet_text.replace("\r", "")
            ).strip()
        else:
            snippet_text = sanitize_snippet_text(snippet_text)
        pairs.append((query, snippet_text))

    try:
        scores = model.predict(pairs, show_progress_bar=False)
    except Exception:
        logger.exception("Cross-encoder rerank failed, falling back to RRF")
        return ranked[:RERANK_LIMIT]

    rescored: list[Snippet] = []
    for snippet, score in zip(ranked[:RERANK_LIMIT], scores, strict=True):
        boosted = float(score)
        header_text = (snippet.header_text or "").lower()
        section_path = (snippet.section_path or "").lower()
        entity_terms = [term.lower() for term in (snippet.entity_terms or [])]
        snippet_text_lower = (snippet.text or "").lower()

        overlap = 0
        for term in profile["lexical_terms"]:
            if term in header_text:
                boosted += rcnf.RERANK_FIELD_WEIGHTS["header_text"]
                overlap += 1
            if term in section_path:
                boosted += rcnf.RERANK_FIELD_WEIGHTS["section_path"]
                overlap += 1
            if term in entity_terms:
                boosted += rcnf.RERANK_FIELD_WEIGHTS["entity_terms"]
                overlap += 1
            if term in snippet_text_lower:
                overlap += 1

        if overlap:
            boosted += min(overlap, 4) * rcnf.RERANK_OVERLAP_WEIGHT

        if profile["table_mode"] and snippet.kind in rcnf.RERANK_TABLE_MODE_BONUS:
            boosted += rcnf.RERANK_TABLE_MODE_BONUS[snippet.kind]

        if snippet.kind in rcnf.RERANK_KIND_BONUS:
            boosted += rcnf.RERANK_KIND_BONUS[snippet.kind]

        if snippet.kind in {"section_summary", "summary"} and overlap == 0:
            boosted -= rcnf.RERANK_SUMMARY_ZERO_OVERLAP_PENALTY

        rescored.append(
            Snippet(**{**asdict(snippet), "rerank_score": round(boosted, 6)})
        )

    rescored.sort(key=lambda item: item.rerank_score or 0.0, reverse=True)
    out: list[Snippet] = []
    seen_by_source_ref: dict[tuple[str, Any], int] = {}
    seen_content_refs: set[tuple[Any, ...]] = set()
    for item in rescored:
        content_key = (
            item.document_id,
            item.kind,
            item.section_path,
            (item.text or "").strip().lower()[:180],
        )
        if content_key in seen_content_refs:
            continue
        seen_content_refs.add(content_key)

        source = str(item.src or "unknown")
        ref = (
            item.document_id
            or item.chat_id
            or item.id
            or item.display_path
            or (item.text or "")[:200]
        )
        key = (source, ref)
        current = seen_by_source_ref.get(key, 0)
        if current >= MAX_SNIPPETS_PER_SOURCE:
            continue
        seen_by_source_ref[key] = current + 1
        out.append(item)
    return out


def trim_messages(
    messages: list[Msg],
    provider: BaseAIProvider | None = None,
    model: ModelInfo | None = None,
    *,
    max_tokens: int,
) -> list[Msg]:
    total_tokens = 0
    trimmed: list[Msg] = []
    for idx, msg in enumerate(reversed(messages)):
        if provider is not None and model is not None:
            tokens = provider.token_count(msg.content, model=model)
        else:
            tokens = token_count(msg.content)
        if total_tokens + tokens > max_tokens and idx > 0:
            break
        total_tokens += tokens
        trimmed.append(msg)
    return list(reversed(trimmed))


def build_context_from_snippets(
    snippets: list[Snippet],
    provider: BaseAIProvider,
    model: ModelInfo,
) -> Msg:
    payload_snippets: list[ContextSnippet] = []
    snippet_tokens = 0
    for snippet in snippets:
        if snippet.kind in {"table", "table_rows"}:
            clean = re.sub(
                r"\n{3,}", "\n\n", (snippet.text or "").replace("\r", "")
            ).strip()
        else:
            clean = sanitize_snippet_text(snippet.text or "")
        if not clean:
            continue
        clean_tokens = provider.token_count(clean, model=model)
        if (
            snippet_tokens + clean_tokens > MAX_CONTEXT_SNIPPET_TOKENS
            and payload_snippets
        ):
            break
        snippet_tokens += clean_tokens
        payload_snippets.append(
            ContextSnippet(
                text=clean,
                kind=snippet.kind,
                src=snippet.src,
                uri=snippet.uri,
                title=snippet.title,
                display_path=snippet.display_path,
                header_text=snippet.header_text,
                section_path=snippet.section_path,
                entity_terms=snippet.entity_terms,
            )
        )
        if len(payload_snippets) >= MAX_CONTEXT_SNIPPETS:
            break

    if not payload_snippets:
        return Msg(
            role="developer",
            content='[context]\n{"type":"rag_context","version":1,"snippets":[]}',
        )

    payload = ContextPayload(snippets=payload_snippets)
    return Msg(
        role="developer",
        content="[context]\n" + json.dumps(asdict(payload), ensure_ascii=False),
    )


def _build_policy_and_coverage(
    prompt_text: str,
    snippets: list[Snippet],
) -> tuple[ContextPolicy, dict[str, Any]]:
    profile = queryprofile(prompt_text)
    has_source_url = any(snippet.uri for snippet in snippets)
    has_quote_candidate = any(
        snippet.uri and snippet.kind in {"text", "summary", "section_summary"}
        for snippet in snippets
    )
    has_table_candidate = any(
        snippet.kind in {"table", "table_rows"} for snippet in snippets
    )
    coverage_refs = {
        (
            snippet.document_id,
            snippet.section_path or snippet.header_text or snippet.title or snippet.id,
        )
        for snippet in snippets
        if snippet.kind in {"table", "table_rows", "text", "section_summary", "summary"}
    }
    has_enumeration_coverage = len(coverage_refs) >= 2

    reason_code = "ok"
    if profile["quote_mode"] and not has_source_url:
        reason_code = "missing_source_url"
    elif profile["quote_mode"] and not has_quote_candidate:
        reason_code = "missing_quote_candidate"
    elif profile["table_mode"] and not has_table_candidate:
        reason_code = "missing_table_candidate"
    elif profile["enumeration_mode"] and not has_enumeration_coverage:
        reason_code = "partial_enumeration"

    policy = ContextPolicy(
        quote_mode=profile["quote_mode"],
        table_mode=profile["table_mode"],
        enumeration_mode=profile["enumeration_mode"],
        has_source_url=has_source_url,
        has_quote_candidate=has_quote_candidate,
        has_table_candidate=has_table_candidate,
        has_enumeration_coverage=has_enumeration_coverage,
        reason_code=reason_code,
    )
    coverage = {
        "snippet_count": len(snippets),
        "source_count": len({snippet.uri for snippet in snippets if snippet.uri}),
        "section_count": len(coverage_refs),
        "table_count": sum(
            1 for snippet in snippets if snippet.kind in {"table", "table_rows"}
        ),
        "quote_ready": has_source_url and has_quote_candidate,
        "enumeration_coverage": has_enumeration_coverage,
    }
    return policy, coverage


def _build_source_payloads(snippets: list[Snippet]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for citation_id, snippet in enumerate(snippets):
        key = (
            snippet.uri,
            snippet.document_id,
            snippet.chunk_ix,
            snippet.section_path,
            snippet.header_text,
            snippet.kind,
        )
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "citation_id": citation_id,
                "uri": snippet.uri,
                "title": snippet.title,
                "display_path": snippet.display_path,
                "kind": snippet.kind,
                "header_text": snippet.header_text,
                "section_path": snippet.section_path,
            }
        )
    return sources


def _build_used_chunks(snippets: list[Snippet]) -> list[dict[str, Any]]:
    used_chunks: list[dict[str, Any]] = []
    for citation_id, snippet in enumerate(snippets):
        used_chunks.append(
            {
                "id": snippet.id,
                "citation_id": citation_id,
                "document_id": snippet.document_id,
                "chunk_ix": snippet.chunk_ix,
                "src": snippet.src,
                "uri": snippet.uri,
                "title": snippet.title,
                "display_path": snippet.display_path,
                "kind": snippet.kind,
                "header_text": snippet.header_text,
                "section_path": snippet.section_path,
                "score": snippet.dist,
                "rerank_score": snippet.rerank_score,
            }
        )
    return used_chunks


async def get_context(
    db: AsyncSession,
    chat_id: str,
    prompt: str,
    *,
    provider: BaseAIProvider,
    model: ModelInfo,
    tail_limit: int = TAIL_MSG_LIMIT,
    vector_top_k: int = VECTOR_TOP_K,
    ft_top_m: int = FT_TOP_M,
) -> ContextResult:
    profile = queryprofile(prompt)
    query_vec = embed_query(prompt)
    tail = await tail_messages(db, chat_id=chat_id, limit=tail_limit)

    async with asyncio.TaskGroup() as task_group:
        vector_task = task_group.create_task(
            vector_supply(
                db=db, chat_id=chat_id, query_vec=query_vec, top_k=vector_top_k
            )
        )
        ft_task = task_group.create_task(
            fulltext_supply(db=db, prompt_text=prompt, top_m=ft_top_m)
        )

    vector_snippets = vector_task.result()
    ft_snippets = ft_task.result()
    snippets = reciprocal_rank_fusion([vector_snippets, ft_snippets])
    if profile["table_mode"]:
        snippets.sort(
            key=lambda item: (
                item.kind not in {"table", "table_rows"},
                -(item.rerank_score or 0.0),
                item.dist if item.dist is not None else float("inf"),
            )
        )
    snippets = crossrerank(prompt, snippets)

    policy, coverage = _build_policy_and_coverage(prompt, snippets)
    policy_msg = Msg(
        role="developer",
        content="[policy]\n"
        + json.dumps(
            {"policy": asdict(policy), "coverage": coverage},
            ensure_ascii=False,
        ),
    )
    context_msg = build_context_from_snippets(snippets, provider=provider, model=model)

    context_msgs = tail + [policy_msg, context_msg, Msg(role="user", content=prompt)]
    context_budget = max(
        2048,
        min(
            MAX_INPUT_CONTEXT_TOKENS,
            model.context_window - model.max_tokens - CONTEXT_SAFETY_MARGIN,
        ),
    )
    context_msgs = trim_messages(
        context_msgs,
        provider=provider,
        model=model,
        max_tokens=context_budget,
    )

    return ContextResult(
        messages=context_msgs,
        used_chunks=_build_used_chunks(snippets),
        sources=_build_source_payloads(snippets),
        policy=asdict(policy),
        coverage=coverage,
    )
