from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vchat.db import async_session_factory
from vchat.models import Chat
from vchat.views.chat.ai import resolve_ai_settings
from vchat.views.chat.ctx import (
    CONTEXT_SAFETY_MARGIN,
    FT_TOP_M,
    MAX_CONTEXT_SNIPPET_TOKENS,
    MAX_CONTEXT_SNIPPETS,
    MAX_INPUT_CONTEXT_TOKENS,
    RERANK_LIMIT,
    TAIL_MSG_LIMIT,
    VECTOR_TOP_K,
    _build_policy_and_coverage,
    _build_source_payloads,
    _build_used_chunks,
    build_context_from_snippets,
    crossrerank,
    embed_query,
    filter_snippets_by_document_relevance,
    fulltext_supply,
    queryprofile,
    reciprocal_rank_fusion,
    select_context_snippets,
    tail_messages,
    trim_messages,
    vector_supply,
    warmup_models,
    Msg,
)
from vchat.settings import config


DEFAULT_SIZES = (16, 64, 256, 1024, 4096, 8192, 16384, 32768)
PROMPT_SEED = (
    "вопрос метод магический метод инструкция документ источник таблица "
    "контекст правило пользовательский запрос "
)


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 3)


def _prompt_for_size(chars: int) -> str:
    if chars <= 0:
        return ""
    return (PROMPT_SEED * ((chars // len(PROMPT_SEED)) + 1))[:chars]


async def _resolve_chat_id(chat_id: str | None) -> str:
    if chat_id:
        return chat_id
    async with async_session_factory() as db:
        found = await db.scalar(sa.select(Chat.id).order_by(Chat.created_at.desc()))
    if not found:
        raise RuntimeError("No local chats found; pass --chat-id explicitly.")
    return str(found)


async def _timed_await(label: str, durations: dict[str, float], awaitable):
    started_at = time.perf_counter()
    result = await awaitable
    durations[label] = _elapsed_ms(started_at)
    return result


async def measure_context_pipeline(
    *,
    chat_id: str,
    prompt: str,
    provider_id: str | None,
    model_id: str | None,
    vector_top_k: int,
    ft_top_m: int,
    tail_limit: int,
) -> dict[str, Any]:
    provider, model = resolve_ai_settings(
        provider_id or config.get("chat_provider"),
        model_id or config.get("chat_model"),
    )
    durations: dict[str, float] = {}

    started_at = time.perf_counter()
    profile = queryprofile(prompt)
    durations["query_profile"] = _elapsed_ms(started_at)

    started_at = time.perf_counter()
    query_vec = embed_query(prompt)
    durations["embed_query"] = _elapsed_ms(started_at)

    async with async_session_factory() as db:
        tail = await _timed_await(
            "tail_messages",
            durations,
            tail_messages(db, chat_id=chat_id, limit=tail_limit),
        )

        async with asyncio.TaskGroup() as task_group:
            vector_task = task_group.create_task(
                _timed_await(
                    "vector_supply",
                    durations,
                    vector_supply(
                        db=db,
                        chat_id=chat_id,
                        query_vec=query_vec,
                        top_k=vector_top_k,
                    ),
                )
            )
            ft_task = task_group.create_task(
                _timed_await(
                    "fulltext_supply",
                    durations,
                    fulltext_supply(db=db, prompt_text=prompt, top_m=ft_top_m),
                )
            )

        vector_snippets = vector_task.result()
        ft_snippets = ft_task.result()

    started_at = time.perf_counter()
    fused_snippets = reciprocal_rank_fusion([vector_snippets, ft_snippets])
    durations["reciprocal_rank_fusion"] = _elapsed_ms(started_at)

    if profile["table_mode"]:
        started_at = time.perf_counter()
        fused_snippets.sort(
            key=lambda item: (
                item.kind not in {"table", "table_rows"},
                -(item.rerank_score or 0.0),
                item.dist if item.dist is not None else float("inf"),
            )
        )
        durations["table_mode_sort"] = _elapsed_ms(started_at)

    started_at = time.perf_counter()
    reranked_snippets = crossrerank(prompt, fused_snippets)
    durations["crossrerank"] = _elapsed_ms(started_at)

    started_at = time.perf_counter()
    filtered_snippets = filter_snippets_by_document_relevance(reranked_snippets)
    durations["document_relevance_filter"] = _elapsed_ms(started_at)

    started_at = time.perf_counter()
    context_snippets = select_context_snippets(
        filtered_snippets,
        provider=provider,
        model=model,
    )
    durations["select_context_snippets"] = _elapsed_ms(started_at)

    started_at = time.perf_counter()
    policy, coverage = _build_policy_and_coverage(prompt, context_snippets)
    durations["policy_and_coverage"] = _elapsed_ms(started_at)

    started_at = time.perf_counter()
    policy_msg = Msg(
        role="developer",
        content="[policy]\n"
        + json.dumps(
            {"policy": asdict(policy), "coverage": coverage},
            ensure_ascii=False,
        ),
    )
    context_msg = build_context_from_snippets(
        context_snippets,
        provider=provider,
        model=model,
    )
    durations["build_context_messages"] = _elapsed_ms(started_at)

    untrimmed_messages = tail + [
        policy_msg,
        context_msg,
        Msg(role="user", content=prompt),
    ]
    context_budget = max(
        2048,
        min(
            MAX_INPUT_CONTEXT_TOKENS,
            model.context_window - model.max_tokens - CONTEXT_SAFETY_MARGIN,
        ),
    )
    started_at = time.perf_counter()
    context_messages = trim_messages(
        untrimmed_messages,
        provider=provider,
        model=model,
        max_tokens=context_budget,
    )
    durations["trim_messages"] = _elapsed_ms(started_at)

    user_prompt_tokens = provider.token_count(prompt, model=model)
    untrimmed_tokens = sum(
        provider.token_count(msg.content, model=model) for msg in untrimmed_messages
    )
    trimmed_tokens = sum(
        provider.token_count(msg.content, model=model) for msg in context_messages
    )
    selected_snippet_tokens = sum(
        provider.token_count(snippet.text or "", model=model)
        for snippet in context_snippets
    )

    return {
        "input": {
            "chars": len(prompt),
            "bytes": len(prompt.encode("utf-8")),
            "tokens": user_prompt_tokens,
        },
        "durations_ms": durations,
        "total_context_prep_ms": round(sum(durations.values()), 3),
        "counts": {
            "tail_messages": len(tail),
            "vector_snippets": len(vector_snippets),
            "fulltext_snippets": len(ft_snippets),
            "fused_snippets": len(fused_snippets),
            "reranked_snippets": len(reranked_snippets),
            "filtered_snippets": len(filtered_snippets),
            "selected_snippets": len(context_snippets),
            "sources": len(_build_source_payloads(context_snippets)),
            "used_chunks": len(_build_used_chunks(context_snippets)),
            "messages_before_trim": len(untrimmed_messages),
            "messages_after_trim": len(context_messages),
        },
        "token_budgets": {
            "context_budget": context_budget,
            "untrimmed_context_tokens": untrimmed_tokens,
            "trimmed_context_tokens": trimmed_tokens,
            "selected_snippet_tokens": selected_snippet_tokens,
        },
        "limit_flags": {
            "rerank_limit_hit": len(fused_snippets) > RERANK_LIMIT,
            "max_context_snippets_hit": (
                len(context_snippets) >= MAX_CONTEXT_SNIPPETS
                and len(filtered_snippets) > len(context_snippets)
            ),
            "context_snippet_token_budget_hit": (
                selected_snippet_tokens >= MAX_CONTEXT_SNIPPET_TOKENS
                and len(filtered_snippets) > len(context_snippets)
            ),
            "context_messages_trimmed": len(context_messages) < len(untrimmed_messages),
            "user_prompt_exceeds_context_budget": user_prompt_tokens > context_budget,
        },
    }


def summarize_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    by_size: dict[int, list[dict[str, Any]]] = {}
    for run in runs:
        by_size.setdefault(int(run["input"]["chars"]), []).append(run)

    rows = []
    for size, items in sorted(by_size.items()):
        stage_names = sorted(
            {name for item in items for name in item["durations_ms"].keys()}
        )
        stage_medians = {
            name: round(
                statistics.median(
                    item["durations_ms"].get(name, 0.0) for item in items
                ),
                3,
            )
            for name in stage_names
        }
        rows.append(
            {
                "chars": size,
                "bytes": items[-1]["input"]["bytes"],
                "tokens": items[-1]["input"]["tokens"],
                "total_context_prep_ms_median": round(
                    statistics.median(
                        item["total_context_prep_ms"] for item in items
                    ),
                    3,
                ),
                "stage_medians_ms": stage_medians,
                "counts": items[-1]["counts"],
                "token_budgets": items[-1]["token_budgets"],
                "limit_flags": items[-1]["limit_flags"],
            }
        )
    return {
        "hard_limits": {
            "aiohttp_app_client_max_size_bytes": config["max_upload_size"],
            "aiohttp_websocket_default_max_msg_size_bytes": 4 * 1024 * 1024,
            "chat_response_max_tokens": int(config.get("chat_response_max_tokens", 900)),
            "max_input_context_tokens": MAX_INPUT_CONTEXT_TOKENS,
            "max_context_snippet_tokens": MAX_CONTEXT_SNIPPET_TOKENS,
            "max_context_snippets": MAX_CONTEXT_SNIPPETS,
            "rerank_limit": RERANK_LIMIT,
            "vector_top_k_default": VECTOR_TOP_K,
            "fulltext_top_m_default": FT_TOP_M,
            "tail_msg_limit_default": TAIL_MSG_LIMIT,
            "embedding_max_seq_length_config": config.get("embedding_max_seq_length"),
            "reranker_max_length": 512,
        },
        "rows": rows,
    }


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "| chars | bytes | tokens | total prep ms | embed ms | vector ms | fts ms | rerank ms | trim? | user>budget? |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|:---:|:---:|",
    ]
    for row in summary["rows"]:
        stages = row["stage_medians_ms"]
        flags = row["limit_flags"]
        lines.append(
            "| {chars} | {bytes} | {tokens} | {total} | {embed} | {vector} | {fts} | {rerank} | {trim} | {over} |".format(
                chars=row["chars"],
                bytes=row["bytes"],
                tokens=row["tokens"],
                total=row["total_context_prep_ms_median"],
                embed=stages.get("embed_query", 0.0),
                vector=stages.get("vector_supply", 0.0),
                fts=stages.get("fulltext_supply", 0.0),
                rerank=stages.get("crossrerank", 0.0),
                trim="yes" if flags["context_messages_trimmed"] else "no",
                over="yes" if flags["user_prompt_exceeds_context_budget"] else "no",
            )
        )
    return "\n".join(lines)


async def _async_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat-id")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--sizes", default=",".join(str(item) for item in DEFAULT_SIZES))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--json-path")
    parser.add_argument("--no-warmup", action="store_true")
    parser.add_argument("--vector-top-k", type=int, default=VECTOR_TOP_K)
    parser.add_argument("--ft-top-m", type=int, default=FT_TOP_M)
    parser.add_argument("--tail-limit", type=int, default=TAIL_MSG_LIMIT)
    args = parser.parse_args()

    sizes = [int(item.strip()) for item in args.sizes.split(",") if item.strip()]
    chat_id = await _resolve_chat_id(args.chat_id)

    warmup_ms = None
    if not args.no_warmup:
        started_at = time.perf_counter()
        warmup_models()
        warmup_ms = _elapsed_ms(started_at)

    runs = []
    for size in sizes:
        prompt = _prompt_for_size(size)
        for _ in range(args.repeats):
            runs.append(
                await measure_context_pipeline(
                    chat_id=chat_id,
                    prompt=prompt,
                    provider_id=args.provider,
                    model_id=args.model,
                    vector_top_k=args.vector_top_k,
                    ft_top_m=args.ft_top_m,
                    tail_limit=args.tail_limit,
                )
            )

    summary = summarize_runs(runs)
    _, selected_model = resolve_ai_settings(
        args.provider or config.get("chat_provider"),
        args.model or config.get("chat_model"),
    )
    summary["hard_limits"]["model_context_window"] = selected_model.context_window
    summary["hard_limits"]["model_max_tokens"] = selected_model.max_tokens
    summary["hard_limits"]["context_safety_margin"] = CONTEXT_SAFETY_MARGIN
    summary["chat_id"] = chat_id
    summary["model_warmup_ms"] = warmup_ms
    summary["repeats"] = args.repeats
    summary["provider"] = args.provider or config.get("chat_provider")
    summary["model"] = args.model or config.get("chat_model")

    print(render_markdown(summary))
    print()
    print(json.dumps(summary["hard_limits"], ensure_ascii=False, indent=2))

    if args.json_path:
        path = Path(args.json_path)
        path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"json_report: {path}")
    return 0


def main() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    raise SystemExit(main())
