from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import requests
from pydantic import BaseModel, Field, ValidationError

from vchat.views.chat.oauth import _normalize_basic_auth, _parse_expires_at, _Token
from vchat.models import Page
from vchat.settings import config
from vchat.views.chat.ai import BaseAIProvider, ModelInfo, resolve_ai_settings

TRIGGER_GENERATION_LIMIT = 10
TRIGGER_CONTENT_CHARS = 8000

logger = logging.getLogger(__name__)
_gigachat_token_cache: _Token | None = None


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


def _get_gigachat_access_token_sync(
    *,
    basic_auth_key: str,
    oauth_url: str | None = None,
    scope: str | None = None,
    verify_ssl_certs: bool | None = None,
    oauth_timeout_seconds: float | None = None,
) -> str:
    global _gigachat_token_cache
    now = time.time()
    margin = 30.0
    if (
        _gigachat_token_cache is not None
        and (_gigachat_token_cache.expires_at - now) > margin
    ):
        return _gigachat_token_cache.access_token

    resolved_oauth_url = (oauth_url or config.get("gigachat_oauth_url") or "").strip()
    if not resolved_oauth_url:
        resolved_oauth_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"

    resolved_scope = (scope or config.get("gigachat_scope") or "").strip()
    if not resolved_scope:
        resolved_scope = "GIGACHAT_API_PERS"

    if verify_ssl_certs is None:
        verify_ssl_certs = bool(config.get("gigachat_verify_ssl_certs", True))

    if oauth_timeout_seconds is None:
        oauth_timeout_seconds = float(
            config.get("gigachat_oauth_timeout_seconds", 15.0)
        )

    auth_header = _normalize_basic_auth(basic_auth_key)
    if not auth_header:
        raise RuntimeError("Missing GigaChat authorization key (Basic)")

    resp = requests.post(
        resolved_oauth_url,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": auth_header,
        },
        data={"scope": resolved_scope},
        verify=bool(verify_ssl_certs),
        timeout=float(oauth_timeout_seconds),
    )
    if resp.status_code >= 400:
        logger.error(
            "GigaChat OAuth failed: status=%s url=%s body=%s",
            resp.status_code,
            resolved_oauth_url,
            (resp.text or "").strip()[:1000],
        )
        raise RuntimeError(
            f"GigaChat OAuth error {resp.status_code}: {resp.text.strip() or 'empty response'}"
        )

    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("GigaChat OAuth returned unexpected payload")

    access_token = (
        payload.get("access_token")
        or payload.get("accessToken")
        or payload.get("token")
    )
    if not isinstance(access_token, str) or not access_token.strip():
        raise RuntimeError("GigaChat OAuth did not return access_token")

    now = time.time()
    expires_at = _parse_expires_at(payload, now=now)
    if expires_at is None:
        expires_at = now + 25 * 60

    _gigachat_token_cache = _Token(
        access_token=access_token.strip(),
        expires_at=float(expires_at),
    )
    return _gigachat_token_cache.access_token


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
        api_key = _get_gigachat_access_token_sync(
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
