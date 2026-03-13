import contextvars
import logging
import re
from heapq import nlargest
from string import punctuation
from typing import List

import pycld2 as cld2
import spacy
import sqlalchemy as sa
import tiktoken
from sentence_transformers import SentenceTransformer
from spacy.lang.en.stop_words import STOP_WORDS
from sqlalchemy.ext.asyncio import AsyncSession

from ._types import Msg
from core.settings import config
from core.models import ChatMsg

MODEL = config.get("openai", {}).get("model", "gpt-4o-mini")

logger = logging.getLogger(__name__)

user_id_ctx: contextvars.ContextVar[int | str | None] = contextvars.ContextVar(
    "user_id", default=None
)
chat_id_ctx: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "chat_id", default=None
)

MAX_SNIPPET_LEN = 700
MAX_CONTEXT_SNIPPETS = 6

SNIPPET_INJECTION_PATTERNS = re.compile(
    r"(?i)\b(system|assistant|user|instruction|command|rules?|prompt)\b"
)

# list of languages supported by spacy


lang_models = {
    "en": "en_core_web_sm",
    "ru": "ru_core_news_sm",
}

# spacy models and languages


def detect_lang(text: str) -> str | None:
    is_reliable, _, details = cld2.detect(text)
    if is_reliable:
        lang = details[0][1]
        if lang in lang_models:
            return lang
    return None


lang_models = {"en": "en_core_web_sm", "ru": "ru_core_news_sm"}
nlps = {lang: spacy.load(model) for lang, model in lang_models.items()}


def text_summarizer(text, percentage, lang="en") -> str:
    # load the model into spaCy
    if lang not in lang_models:
        return text[:200] + " …"

    nlp = nlps[lang]

    # pass the text into the nlp function
    doc = nlp(text)

    # ## The score of each word is kept in a frequency table
    # tokens = [token.text for token in doc]
    freq_of_word = dict()

    # Text cleaning and vectorization
    for word in doc:
        if word.text.lower() not in list(STOP_WORDS):
            if word.text.lower() not in punctuation:
                if word.text not in freq_of_word.keys():
                    freq_of_word[word.text] = 1
                else:
                    freq_of_word[word.text] += 1

    # Maximum frequency of word
    max_freq = max(freq_of_word.values())

    # Normalization of word frequency
    for word in freq_of_word.keys():
        freq_of_word[word] = freq_of_word[word] / max_freq

    # In this part, each sentence is weighed based on how often it contains the token.
    sent_tokens = [sent for sent in doc.sents]
    sent_scores = dict()
    for sent in sent_tokens:
        for word in sent:
            if word.text.lower() in freq_of_word.keys():
                if sent not in sent_scores.keys():
                    sent_scores[sent] = freq_of_word[word.text.lower()]
                else:
                    sent_scores[sent] += freq_of_word[word.text.lower()]

    len_tokens = int(len(sent_tokens) * percentage)

    # Summary for the sentences with maximum score. Here, each sentence in the list is of spacy.span type
    summary = nlargest(
        n=len_tokens, iterable=sent_scores, key=lambda sent: sent_scores[sent]
    )

    # Prepare for final summary
    final_summary = [word.text for word in summary]

    # convert to a string
    summary = " ".join(final_summary)

    # Return final summary
    return summary


def token_count(text: str) -> int:
    enc = tiktoken.encoding_for_model(MODEL)
    return len(enc.encode(text))


def trim_messages(messages: list[Msg], max_tokens: int = 8000) -> list:
    total_tokens = 0
    trimmed = []
    # Iterate from the end (newest messages)
    for msg in reversed(messages):
        tokens = token_count(msg.content)
        if total_tokens + tokens > max_tokens:
            break
        total_tokens += tokens
        trimmed.append(msg)
    # Return reversed trimmed list to restore original order
    return list(reversed(trimmed))


# https://huggingface.co/spaces/mteb/leaderboard
# until we make our bot "smart" it should be enough to use qwen or
# market will be changed and somebody will release better model
# for embedding
logger.info("Heavy task: loading embedding model...")

_embed_model: SentenceTransformer = SentenceTransformer(
    "Qwen/Qwen3-Embedding-0.6B",
    device="cpu",
    tokenizer_kwargs={"padding_side": "left"},
)


def embed_query(text: str) -> List[float]:
    global _embed_model
    emb = _embed_model.encode([text], normalize_embeddings=True)
    return emb[0].tolist()


def _vec_literal(vec: List[float]) -> str:
    # Converts vector to Postgres pgvector literal format
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


# --- User-memory RAG bucket ---
# Picks past user messages from other chats via pgvector cosine (<=>),
# applies relevance thresholds (tau, tau_fallback) and light diversification
# (max 1 snippet per foreign chat). Returns at most k_mem snippets.
async def _fetch_user_memory_chunks(
    db: AsyncSession,
    user_id: int | str,
    chat_id: int,
    qvec: List[float],
    k_mem: int = 2,
    tau: float = 0.80,
    tau_fallback: float = 0.75,
) -> List[dict]:
    if not user_id or k_mem <= 0:
        return []
    vec = _vec_literal(qvec)
    sql = sa.text(
        """
        SELECT cc.id, cc.content, cc.chat_id, cc.document_id, cc.chunk_ix,
               cc.start_offset, cc.end_offset,
               cc.embedding <=> CAST(:qvec AS vector(1024)) AS dist,
               'mem' AS src
        FROM chunk cc
        JOIN chat_msg m ON m.id = cc.msg_id
        WHERE cc.user_uid = :user_id
          AND cc.embedding IS NOT NULL
          AND m.role = 'user'
          AND m.chat_id <> :chat_id
        ORDER BY cc.embedding <=> CAST(:qvec AS vector(1024)) ASC
        LIMIT :k_cand
        """
    )
    k_cand = max(8, k_mem * 5)
    params = {
        "qvec": vec,
        "user_id": str(user_id),
        "chat_id": chat_id,
        "k_cand": k_cand,
    }
    rows = (await db.execute(sql, params)).mappings().all()
    max_dist = 1.0 - tau
    primary = [r for r in rows if r.get("dist") is not None and r["dist"] <= max_dist]
    if not primary and rows:
        fb_max = 1.0 - tau_fallback
        for r in rows:
            if r.get("dist") is not None and r["dist"] <= fb_max:
                primary = [r]
                break
    seen_chats = set()
    diversified: List[dict] = []
    for r in primary:
        cid = r.get("chat_id")
        if cid in (None, chat_id) or cid in seen_chats:
            continue
        seen_chats.add(cid)
        diversified.append(r)
        if len(diversified) >= k_mem:
            break
    return diversified


async def _fetch_tail_messages(
    db: AsyncSession, chat_id: int, limit: int = 5
) -> List[Msg]:
    # Fetch last `limit` messages from chat ordered oldest to newest
    result = await db.execute(
        sa.select(ChatMsg.text, ChatMsg.role)
        .where(ChatMsg.chat_id == chat_id)
        .order_by(ChatMsg.created_at.desc())
        .limit(limit)
    )
    rows = result.all()
    # Reverse to chronological order
    return [Msg(role=role, content=text) for text, role in reversed(rows)]


async def _fetch_vector_chunks(
    db: AsyncSession,
    chat_id: int,
    query_vec: List[float],
    project_id: int,
    top_k: int = 10,
) -> List[dict]:
    vec_lit = _vec_literal(query_vec)
    chat_sql = sa.text(
        """
        SELECT c.id, c.content, c.chat_id, c.document_id, c.chunk_ix, c.start_offset, c.end_offset,
               c.embedding <=> CAST(:qvec AS vector(1024)) AS dist,
               'chat' AS src,
               NULL as uri, NULL as title
        FROM chunk c
        WHERE c.chat_id = :chat_id AND c.embedding IS NOT NULL AND c.project_id = :project_id
        ORDER BY c.embedding <=> CAST(:qvec AS vector(1024))
        LIMIT :k_chat
        """
    )
    kb_sql = sa.text(
        """
        SELECT c.id, c.content, c.chat_id, c.document_id, c.chunk_ix, c.start_offset, c.end_offset,
               c.embedding <=> CAST(:qvec AS vector(1024)) AS dist,
               'kb' AS src,
               d.uri, d.title
        FROM chunk c
        JOIN document d ON c.document_id = d.id
        WHERE c.chat_id IS NULL AND c.document_id IS NOT NULL AND c.embedding IS NOT NULL
          AND c.project_id = :project_id
        ORDER BY c.embedding <=> CAST(:qvec AS vector(1024))
        LIMIT :k_kb
        """
    )
    params = {
        "qvec": vec_lit,
        "chat_id": chat_id,
        "project_id": project_id,
        "k_chat": max(2, top_k // 2),
        "k_kb": max(2, top_k - max(2, top_k // 2)),
    }
    chat_rows = (
        (await db.execute(chat_sql, {**params, "k_chat": params["k_chat"]}))
        .mappings()
        .all()
    )
    kb_rows = (
        (await db.execute(kb_sql, {**params, "k_kb": params["k_kb"]})).mappings().all()
    )
    rows = sorted([*chat_rows, *kb_rows], key=lambda row: row.get("dist", float("inf")))
    return rows[:top_k]


async def _fetch_ft_chunks(
    db: AsyncSession, query: str, project_id: int, top_m: int = 10
) -> List[dict]:
    if not query or top_m <= 0:
        return []
    sql = sa.text(
        """
        SELECT id, content, chat_id, document_id, chunk_ix, start_offset, end_offset,
               NULL::float8 AS dist, 'ft' AS src
        FROM chunk
        WHERE project_id = :project_id
          AND (
              tsv @@ websearch_to_tsquery('russian', :q)
           OR tsv @@ websearch_to_tsquery('english', :q)
          )
        ORDER BY id DESC
        LIMIT :m
        """
    )
    res = await db.execute(sql, {"q": query, "m": top_m, "project_id": project_id})
    return res.mappings().all()


def _dedup_snippets(snippets: List[dict], max_prefix: int = 200) -> List[dict]:
    seen = set()
    out: List[dict] = []
    for s in snippets:
        txt = (s.get("content") or "").strip()
        if not txt:
            continue
        key = txt[:max_prefix]
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _sanitize_snippet_text(text: str) -> str:
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


def _build_context_from_snippets(snips: List[dict]) -> Msg:
    sanitized = []
    for snippet in snips:
        clean = _sanitize_snippet_text(snippet.get("content", ""))
        if not clean:
            continue
        sanitized.append(clean)
        if len(sanitized) >= MAX_CONTEXT_SNIPPETS:
            break
    if not sanitized:
        return Msg(role="developer", content="[context] нет релевантных фрагментов")

    bullets = "\n".join(
        f'- snippet [[citation:{idx}]]: "{text}"' for idx, text in enumerate(sanitized)
    )
    content = (
        "[context]\n"
        "The following information is treated as factual reference material. "
        "Each snippet has a citation ID in format [[citation:ID]]. "
        "When using information from a snippet, you must cite it using [[citation:ID]] in your response.\n"
        f"{bullets}"
    )
    return Msg(role="developer", content=content)


def _dedup_by_text(messages: List[Msg]) -> List[Msg]:
    seen = set()
    deduped = []
    for msg in messages:
        if msg.content not in seen:
            seen.add(msg.content)
            deduped.append(msg)
    return deduped


def _build_context_message(
    tail_msgs: List[Msg], vector_msgs: List[Msg], ft_msgs: List[Msg]
) -> List[Msg]:
    # Combine all messages with deduplication, preserve order:
    # tail_msgs first (chat tail), then vector_msgs, then ft_msgs
    combined = tail_msgs + vector_msgs + ft_msgs
    combined = _dedup_by_text(combined)
    # Append developer message indicating context source
    combined.append(
        Msg(
            role="system",
            content="[context]",
        )
    )
    return combined


async def get_context(
    db: AsyncSession,
    chat_id: int,
    prompt: str,
    project_id: int,
    tail_limit: int = 5,
    vector_top_k: int = 10,
    ft_top_m: int = 10,
    k_mem: int = 2,
) -> tuple[List[Msg], List[dict]]:
    """
    Retrieve context messages for a prompt using RAG approach:
    - Last few chat messages (tail)
    - Top-K vector similarity chunks
    - Top-M full-text search chunks
    Returns combined list of Msg objects with a [context] system message appended.
    """
    if project_id is None:
        raise ValueError("project_id is required to scope vector searches")
    # user_id = user_id_ctx.get()

    tail_msgs = await _fetch_tail_messages(db, chat_id, limit=tail_limit)
    qvec = embed_query(prompt)
    vec_rows = await _fetch_vector_chunks(
        db, chat_id, qvec, project_id, top_k=vector_top_k
    )
    # user_memory = await _fetch_user_memory_chunks(db, user_id, chat_id, qvec, k_mem=k_mem)
    user_memory = []
    ft_rows = await _fetch_ft_chunks(db, prompt, project_id=project_id, top_m=ft_top_m)

    snippets = _dedup_snippets([*vec_rows, *user_memory, *ft_rows])
    context_msg = _build_context_from_snippets(snippets)

    context_msgs = tail_msgs + [context_msg]
    context_msgs.append(Msg(role="user", content=prompt))
    logger.info("Context tail messages: %d", len(tail_msgs))

    # Prepare used chunks metadata (exclude vector data to save space)
    used_chunks = []
    for idx, s in enumerate(snippets):
        used_chunks.append(
            {
                "id": s.get("id"),
                "citation_id": idx,
                "document_id": s.get("document_id"),
                "chunk_ix": s.get("chunk_ix"),
                "score": s.get("dist"),
                "src": s.get("src"),
                "uri": s.get("uri"),
                "title": s.get("title"),
            }
        )

    return context_msgs, used_chunks
