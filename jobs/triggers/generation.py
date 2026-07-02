from __future__ import annotations

import logging
from typing import Any

import requests
from pydantic import BaseModel, Field, ValidationError

from vchat.views.chat.oauth import get_gigachat_access_token_sync
from vchat.models import Page
from vchat.settings import config
from vchat.tracing import request_id_headers
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
    provider_id = config.get("chat_provider")
    model_id = config.get("chat_model")
    provider, model = resolve_ai_settings(provider_id, model_id)
    raw = request_trigger_generation(
        provider, model, build_trigger_generation_messages(page)
    )
    return parse_generated_trigger_texts(raw)


def request_trigger_generation(
    provider: BaseAIProvider,
    model: ModelInfo,
    messages: list[dict[str, str]],
) -> str:
    if not getattr(provider, "supports_chat", True):
        raise RuntimeError(f"Provider '{provider.id}' does not support chat")
    meta = provider.request_meta()
    api_key = meta.get("api_key")
    base_url = meta.get("base_url")
    if provider.id == "openai":
        api_key = api_key or config.get("openai_api_key")
        base_url = base_url or config.get(
            "openai_base_url", "https://api.openai.com/v1"
        )
    if not api_key:
        raise RuntimeError("Missing API key for trigger generation")
    if not base_url:
        raise RuntimeError("Missing base URL for trigger generation")

    timeout_seconds = 30.0
    verify_ssl = True
    if provider.id == "gigachat":
        api_key = get_gigachat_access_token_sync(
            basic_auth_key=api_key,
            oauth_timeout_seconds=float(
                config.get("gigachat_oauth_timeout_seconds", 15.0)
            ),
        )
        timeout_seconds = float(config.get("gigachat_request_timeout_seconds", 60.0))
        verify_ssl = bool(config.get("gigachat_verify_ssl_certs", True))

    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={
            **request_id_headers(),
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model.id,
            "messages": messages,
            "temperature": 0.4,
            "max_tokens": 600,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "trigger_generation_response",
                    "schema": _trigger_response_schema(),
                    "strict": True,
                },
            },
        },
        timeout=timeout_seconds,
        verify=verify_ssl,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Trigger generation failed: {resp.status_code} {resp.text}")
    data = resp.json()
    return str(data["choices"][0]["message"]["content"])
