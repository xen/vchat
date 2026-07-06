from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from vchat.models import Page
from vchat.settings import cfg
from vchat.views.chat.ai import BaseAIProvider, ModelInfo, resolve_ai_settings

TRIGGER_GENERATION_LIMIT = 10
TRIGGER_CONTENT_CHARS = 8000

logger = logging.getLogger(__name__)


class TriggerGenerationResponse(BaseModel):
    triggers: list[str] = Field(default_factory=list)


def _clean_trigger_texts(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = " ".join(str(item).split()).strip()
        if not text or text in seen:
            continue
        words = text.split()
        if len(words) > 10:
            text = " ".join(words[:10])
        seen.add(text)
        result.append(text)
    return result[:TRIGGER_GENERATION_LIMIT]


def parse_generated_trigger_texts(raw: str) -> list[str]:
    try:
        parsed = TriggerGenerationResponse.model_validate_json(raw.strip())
    except ValidationError as exc:
        raise ValueError("Trigger generation response must match schema") from exc
    return _clean_trigger_texts(parsed.triggers)


def _trigger_response_schema() -> dict[str, Any]:
    return TriggerGenerationResponse.model_json_schema()


def build_trigger_generation_messages(page: Page) -> list[dict[str, str]]:
    title = " ".join((page.title or "").split()).strip() or "Без названия"
    content = " ".join((page.content or "").split()).strip()
    if len(content) > TRIGGER_CONTENT_CHARS:
        content = content[:TRIGGER_CONTENT_CHARS]
    return [
        {
            "role": "system",
            "content": (
                "Ты генерируешь короткие приглашения для чат-виджета. "
                'Верни только JSON-объект по схеме: {"triggers": ["..."]}. '
                "Каждая строка должна быть до 10 слов, естественной и релевантной странице."
            ),
        },
        {
            "role": "user",
            "content": (
                "Сделай 10 зазывающих приглашений начать диалог по содержимому страницы.\n\n"
                f"URL: {page.uri or ''}\n"
                f"Title: {title}\n"
                f"Content:\n{content}"
            ),
        },
    ]


def generate_trigger_texts_for_page(page: Page) -> list[str]:
    provider, model = resolve_ai_settings(
        cfg.chat_suggestions_provider,
        cfg.chat_suggestions_model,
    )
    raw = request_trigger_generation(
        provider, model, build_trigger_generation_messages(page)
    )
    return parse_generated_trigger_texts(raw)


def request_trigger_generation(
    provider: BaseAIProvider,
    model: ModelInfo,
    messages: list[dict[str, str]],
) -> str:
    return provider.request_chat_completion_sync(
        model=model,
        messages=messages,
        temperature=0.4,
        max_tokens=600,
        response_format=provider.structured_json_response_format(
            name="trigger_generation_response",
            schema=_trigger_response_schema(),
        ),
    )
