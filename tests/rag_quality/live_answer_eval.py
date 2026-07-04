from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.rag_quality import answer_eval
from vchat.views.chat.ai import get_default_provider_id, resolve_ai_settings
from vchat.views.chat import views as chat_views


def select_cases(
    cases: list[dict[str, Any]],
    *,
    names: list[str],
    limit: int,
    run_all: bool,
) -> list[dict[str, Any]]:
    selected = cases
    if names:
        wanted = set(names)
        selected = [
            case
            for case in selected
            if case["name"] in wanted or case["case_type"] in wanted
        ]
        missing = wanted - {case["name"] for case in selected} - {
            case["case_type"] for case in selected
        }
        if missing:
            raise ValueError(f"Unknown eval case or case_type: {sorted(missing)}")
    elif not run_all:
        selected = selected[:limit]
    if not selected:
        raise ValueError("No eval cases selected")
    return selected


async def run_live_case(
    case: dict[str, Any],
    generation_context: chat_views.GenerationContext,
) -> dict[str, Any]:
    events = [
        event
        async for event in chat_views.ai_chat_stream(
            answer_eval.context_and_user_messages(case),
            generation_context,
        )
    ]
    assistant_events = [
        event for event in events if event.get("event") == "assistant_message"
    ]
    if not assistant_events:
        raise AssertionError(f"{case['name']}: missing assistant_message event")

    answer = assistant_events[-1]["message"].get("content") or ""
    answer_eval.assert_grounded_answer(case, answer)
    return {
        "case": case["name"],
        "case_type": case["case_type"],
        "answer": answer,
        "event_count": len(events),
    }


async def run_live_eval(args: argparse.Namespace) -> list[dict[str, Any]]:
    provider_id = args.provider or chat_views.cfg.chat_provider or get_default_provider_id()
    model_id = args.model or chat_views.cfg.chat_model
    provider, model = resolve_ai_settings(provider_id, model_id)
    generation_context = chat_views.GenerationContext(
        provider=provider,
        model=model,
        system_prompt=chat_views.SYSTEM_PROMPT,
    )
    selected_cases = select_cases(
        answer_eval.load_cases(),
        names=args.case,
        limit=args.limit,
        run_all=args.all,
    )
    return [
        await run_live_case(case, generation_context)
        for case in selected_cases
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run opt-in live RAG answer grounding evals."
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="Case name or case_type to run. Can be repeated.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1,
        help="Number of fixture cases to run when --case/--all are omitted.",
    )
    parser.add_argument("--all", action="store_true", help="Run all fixture cases.")
    parser.add_argument("--provider", help="Provider id. Defaults to chat config.")
    parser.add_argument("--model", help="Model id. Defaults to chat config.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    results = asyncio.run(run_live_eval(args))
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
