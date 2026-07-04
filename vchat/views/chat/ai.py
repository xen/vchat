from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, ClassVar, Iterable

import aiohttp
import requests
import tiktoken
from pydantic import BaseModel, Field, ValidationError, field_validator

from vchat.settings import cfg
from vchat.tracing import request_id_headers


@dataclass(frozen=True)
class _GigaChatToken:
    access_token: str
    expires_at: float


_gigachat_token_cache: _GigaChatToken | None = None
_gigachat_token_lock = asyncio.Lock()


def _normalize_gigachat_basic_auth(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("basic "):
        return raw
    return f"Basic {raw}"


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_gigachat_expires_at(payload: dict[str, Any], *, now: float) -> float:
    for key in ("expires_at", "expiresAt", "exp"):
        if key not in payload:
            continue
        value = _coerce_float(payload.get(key))
        if value is None:
            continue
        if value > 1e12:
            value = value / 1000.0
        if value < 1e10 and value < now - 60:
            return now + value
        return value

    for key in ("expires_in", "expiresIn"):
        if key not in payload:
            continue
        value = _coerce_float(payload.get(key))
        if value is not None:
            return now + value

    raise RuntimeError("GigaChat token response did not include expiration")


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    context_window: int
    max_tokens: int
    tokenizer_name: str | None = None


class SuggestedActionsPayload(BaseModel):
    actions: list[str] = Field(default_factory=list)

    @field_validator("actions")
    @classmethod
    def normalize_actions(cls, actions: list[Any]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_action in actions:
            if not isinstance(raw_action, str):
                continue
            action = raw_action.strip()
            if not action:
                continue
            if len(action) > 180:
                action = action[:177].rstrip() + "..."
            key = action.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(action)
            if len(normalized) >= 3:
                break
        return normalized


def suggested_actions_response_format() -> dict[str, Any]:
    schema = SuggestedActionsPayload.model_json_schema()
    schema["required"] = ["actions"]
    schema["additionalProperties"] = False
    actions_schema = schema.get("properties", {}).get("actions")
    if isinstance(actions_schema, dict):
        actions_schema["maxItems"] = 3
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "suggested_actions",
            "strict": True,
            "schema": schema,
        },
    }


def suggested_actions_from_payload(payload: Any) -> list[str]:
    if isinstance(payload, list):
        payload = {"actions": payload}
    elif (
        isinstance(payload, dict)
        and "actions" not in payload
        and "follow_ups" in payload
    ):
        payload = {"actions": payload.get("follow_ups")}
    try:
        parsed = SuggestedActionsPayload.model_validate(payload)
    except ValidationError:
        return []
    return parsed.actions


SUGGESTIONS_PROMPT_CONTEXT_TEMPLATE = """Последний вопрос пользователя:
{{user_question}}

Финальный ответ ассистента:
{{assistant_answer}}

Использованные источники:
{{sources}}
"""


def _truncate_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return f"{text[:head].rstrip()}\n...\n{text[-tail:].lstrip()}"


def _format_suggestion_sources(sources: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, source in enumerate(sources[:5], start=1):
        title = str(source.get("title") or source.get("uri") or "").strip()
        uri = str(source.get("uri") or "").strip()
        if not title and not uri:
            continue
        if uri and uri != title:
            lines.append(f"{index}. {title} — {uri}")
        else:
            lines.append(f"{index}. {title or uri}")
    return "\n".join(lines) or "Источники не использовались."


def render_suggestions_prompt(
    *,
    template: str,
    user_text: str,
    assistant_text: str,
    sources: list[dict[str, Any]],
) -> str:
    values = {
        "{{user_question}}": _truncate_middle(
            user_text,
            cfg.chat_suggestions_max_context_chars,
        ),
        "{{assistant_answer}}": _truncate_middle(
            assistant_text,
            cfg.chat_suggestions_max_context_chars,
        ),
        "{{sources}}": _truncate_middle(
            _format_suggestion_sources(sources),
            cfg.chat_suggestions_max_context_chars,
        ),
    }
    rendered = template
    for marker, value in values.items():
        rendered = rendered.replace(marker, value)
    context_prompt = SUGGESTIONS_PROMPT_CONTEXT_TEMPLATE
    for marker, value in values.items():
        context_prompt = context_prompt.replace(marker, value)
    return "\n\n".join(
        part.strip() for part in [rendered, context_prompt] if part.strip()
    )


class BaseAIProvider:
    id: ClassVar[str]
    title: ClassVar[str]
    supports_chat: ClassVar[bool] = True

    def __init__(self) -> None:
        self._api_key = self._load_api_key()

    def _load_api_key(self) -> str | None:
        return None

    @property
    def api_key(self) -> str | None:
        return self._api_key

    @property
    def base_url(self) -> str | None:
        return None

    @property
    def models(self) -> list[ModelInfo]:
        return []

    def get_model(self, model_id: str) -> ModelInfo:
        items = self.models
        if not items:
            raise ValueError(f"Provider '{self.id}' has no models configured")
        if not model_id:
            raise ValueError(f"Missing model for provider '{self.id}'")
        for item in items:
            if item.id == model_id:
                return item
        raise ValueError(f"Unknown model '{model_id}' for provider '{self.id}'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "models": [model.__dict__ for model in self.models],
        }

    def request_meta(self) -> dict[str, Any]:
        return {
            "api_key": self.api_key,
            "base_url": self.base_url,
        }

    @property
    def chat_completion_timeout_seconds(self) -> float:
        return 30.0

    @property
    def chat_completion_verify_ssl_certs(self) -> bool:
        return True

    def chat_completion_bearer_token_sync(self) -> str:
        if not self.api_key:
            raise RuntimeError(f"Missing API key for provider '{self.id}'")
        return self.api_key

    async def chat_completion_bearer_token(
        self,
        session: aiohttp.ClientSession,
    ) -> str:
        _ = session
        return self.chat_completion_bearer_token_sync()

    @property
    def chat_suggestion_timeout_seconds(self) -> float:
        return 10.0

    @property
    def chat_completion_temperature(self) -> float:
        return 0.2

    @property
    def chat_completion_max_tokens(self) -> int:
        return 250

    @property
    def chat_completion_response_format(self) -> dict[str, Any]:
        return suggested_actions_response_format()

    @property
    def chat_completion_format_instruction(self) -> str:
        return ""

    def parse_chat_completion_payload(self, payload: dict[str, Any]) -> list[str]:
        content = str(payload["choices"][0]["message"]["content"])
        normalized = content.strip()
        if normalized.startswith("```"):
            normalized = normalized.strip("`json \n")
        suggestions = suggested_actions_from_payload(json.loads(normalized))
        if not suggestions:
            raise ValueError("Suggestion payload does not contain actions")
        return suggestions

    async def request_chat_completion(
        self,
        *,
        ctx: Any,
        user_text: str,
        assistant_text: str,
        sources: list[dict[str, Any]],
    ) -> list[str]:
        if not self.supports_chat:
            raise RuntimeError(f"Provider '{self.id}' does not support chat")
        if not self.base_url:
            raise RuntimeError(f"Missing base URL for provider '{self.id}'")

        rendered_prompt = render_suggestions_prompt(
            template=ctx.suggestions_prompt,
            user_text=user_text,
            assistant_text=assistant_text,
            sources=sources,
        )
        if self.chat_completion_format_instruction:
            rendered_prompt = "\n\n".join(
                [rendered_prompt, self.chat_completion_format_instruction]
            )
        payload: dict[str, Any] = {
            "model": ctx.suggestions_model.id,
            "messages": [{"role": "user", "content": rendered_prompt}],
            "temperature": self.chat_completion_temperature,
            "max_tokens": self.chat_completion_max_tokens,
        }
        if self.chat_completion_response_format:
            payload["response_format"] = self.chat_completion_response_format

        async with aiohttp.ClientSession() as session:
            token = await self.chat_completion_bearer_token(session)
            async with session.post(
                f"{self.base_url}/chat/completions",
                headers={
                    **request_id_headers(),
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(
                    total=self.chat_suggestion_timeout_seconds
                ),
                ssl=self.chat_completion_verify_ssl_certs,
            ) as resp:
                if resp.status >= 400:
                    error_text = await resp.text()
                    raise aiohttp.ClientResponseError(
                        request_info=resp.request_info,
                        history=resp.history,
                        status=resp.status,
                        message=error_text,
                        headers=resp.headers,
                    )

                data = await resp.json()
                return self.parse_chat_completion_payload(data)

    def request_chat_completion_sync(
        self,
        *,
        model: ModelInfo,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        if not self.supports_chat:
            raise RuntimeError(f"Provider '{self.id}' does not support chat")
        if not self.base_url:
            raise RuntimeError(f"Missing base URL for provider '{self.id}'")

        payload: dict[str, Any] = {
            "model": model.id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                **request_id_headers(),
                "Authorization": f"Bearer {self.chat_completion_bearer_token_sync()}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.chat_completion_timeout_seconds,
            verify=self.chat_completion_verify_ssl_certs,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Provider '{self.id}' chat completion failed: "
                f"{resp.status_code} {resp.text}"
            )
        data = resp.json()
        return str(data["choices"][0]["message"]["content"])

    def token_count(self, text: str, model: ModelInfo | None = None) -> int:
        target = model or (self.models[0] if self.models else None)
        tokenizer_name = target.tokenizer_name if target else None
        try:
            if tokenizer_name:
                enc = tiktoken.get_encoding(tokenizer_name)
            elif target:
                enc = tiktoken.encoding_for_model(target.id)
            else:
                enc = tiktoken.get_encoding("cl100k_base")
        except (KeyError, ValueError):
            enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text or ""))


class OpenAIProvider(BaseAIProvider):
    id = "openai"
    title = "OpenAI"

    _models: ClassVar[list[ModelInfo]] = [
        ModelInfo(
            "gpt-4o-mini", "GPT-4o mini", context_window=128000, max_tokens=16384
        ),
        ModelInfo("gpt-4o", "GPT-4o", context_window=128000, max_tokens=16384),
        ModelInfo(
            "gpt-3.5-turbo", "GPT-3.5 Turbo", context_window=16384, max_tokens=4096
        ),
    ]

    @property
    def models(self) -> list[ModelInfo]:
        return list(self._models)

    def _load_api_key(self) -> str | None:
        return cfg.openai_api_key

    @property
    def base_url(self) -> str:
        return cfg.openai_base_url


class YandexGPTProvider(BaseAIProvider):
    id = "yandex"
    title = "YandexGPT"
    supports_chat = False

    _models: ClassVar[list[ModelInfo]] = [
        ModelInfo(
            "yandexgpt-lite",
            "YandexGPT Lite",
            context_window=32768,
            max_tokens=4096,
            tokenizer_name="cl100k_base",
        ),
        ModelInfo(
            "yandexgpt-pro",
            "YandexGPT Pro",
            context_window=32768,
            max_tokens=4096,
            tokenizer_name="cl100k_base",
        ),
    ]

    @property
    def models(self) -> list[ModelInfo]:
        return list(self._models)

    def _load_api_key(self) -> str | None:
        return cfg.yandex_api_key

    @property
    def base_url(self) -> str | None:
        return cfg.yandex_base_url


class GigaChatProvider(BaseAIProvider):
    id = "gigachat"
    title = "GigaChat"
    supports_chat = True

    _models: ClassVar[list[ModelInfo]] = [
        ModelInfo(
            "GigaChat-2",
            "GigaChat 2 Lite",
            context_window=128000,
            max_tokens=4096,
            tokenizer_name="cl100k_base",
        ),
        ModelInfo(
            "GigaChat-2-Pro",
            "GigaChat 2 Pro",
            context_window=128000,
            max_tokens=4096,
            tokenizer_name="cl100k_base",
        ),
        ModelInfo(
            "GigaChat-2-Max",
            "GigaChat 2 Max",
            context_window=128000,
            max_tokens=4096,
            tokenizer_name="cl100k_base",
        ),
    ]

    @property
    def models(self) -> list[ModelInfo]:
        if isinstance(cfg.gigachat_models, list) and cfg.gigachat_models:
            items: list[ModelInfo] = []
            for entry in cfg.gigachat_models:
                if isinstance(entry, str) and entry.strip():
                    items.append(
                        ModelInfo(
                            entry.strip(),
                            entry.strip(),
                            context_window=128000,
                            max_tokens=4096,
                            tokenizer_name="cl100k_base",
                        )
                    )
                    continue

                if isinstance(entry, dict):
                    model_id = str(entry.get("id") or "").strip()
                    if not model_id:
                        continue
                    label = str(entry.get("label") or model_id).strip()
                    context_window = int(entry.get("context_window") or 32768)
                    max_tokens = int(entry.get("max_tokens") or 4096)
                    tokenizer_name = entry.get("tokenizer_name")
                    if tokenizer_name is not None:
                        tokenizer_name = str(tokenizer_name).strip() or None

                    items.append(
                        ModelInfo(
                            model_id,
                            label,
                            context_window=context_window,
                            max_tokens=max_tokens,
                            tokenizer_name=tokenizer_name or "cl100k_base",
                        )
                    )

            if items:
                return items

        return list(self._models)

    def _load_api_key(self) -> str | None:
        return cfg.gigachat_api_key

    @property
    def base_url(self) -> str | None:
        return cfg.gigachat_base_url

    @property
    def chat_completion_timeout_seconds(self) -> float:
        return cfg.gigachat_request_timeout_seconds

    @property
    def chat_completion_verify_ssl_certs(self) -> bool:
        return cfg.gigachat_verify_ssl_certs

    def chat_completion_bearer_token_sync(self) -> str:
        if not self.api_key:
            raise RuntimeError("Missing GigaChat authorization key (Basic)")
        return self._request_access_token_sync()

    def _request_access_token_sync(self) -> str:
        global _gigachat_token_cache
        now = time.time()
        token = _gigachat_token_cache
        if token is not None and (token.expires_at - now) > 30.0:
            return token.access_token

        auth_header = _normalize_gigachat_basic_auth(self.api_key or "")
        if not auth_header:
            raise RuntimeError("Missing GigaChat authorization key (Basic)")

        resp = requests.post(
            cfg.gigachat_oauth_url.strip(),
            headers={
                **request_id_headers(),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
                "Authorization": auth_header,
            },
            data={"scope": cfg.gigachat_scope.strip()},
            verify=cfg.gigachat_verify_ssl_certs,
            timeout=cfg.gigachat_oauth_timeout_seconds,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"GigaChat token request failed {resp.status_code}: "
                f"{resp.text.strip() or 'empty response'}"
            )

        payload = resp.json()
        if not isinstance(payload, dict):
            raise RuntimeError("GigaChat token response must be a JSON object")
        access_token = (
            payload.get("access_token")
            or payload.get("accessToken")
            or payload.get("token")
        )
        if not isinstance(access_token, str) or not access_token.strip():
            raise RuntimeError("GigaChat token response did not include access_token")

        _gigachat_token_cache = _GigaChatToken(
            access_token=access_token.strip(),
            expires_at=_parse_gigachat_expires_at(payload, now=time.time()),
        )
        return _gigachat_token_cache.access_token

    async def chat_completion_bearer_token(
        self,
        session: aiohttp.ClientSession,
    ) -> str:
        if not self.api_key:
            raise RuntimeError("Missing GigaChat authorization key (Basic)")
        return await self._request_access_token(session)

    async def _request_access_token(self, session: aiohttp.ClientSession) -> str:
        global _gigachat_token_cache
        now = time.time()
        token = _gigachat_token_cache
        if token is not None and (token.expires_at - now) > 30.0:
            return token.access_token

        async with _gigachat_token_lock:
            now = time.time()
            token = _gigachat_token_cache
            if token is not None and (token.expires_at - now) > 30.0:
                return token.access_token

            auth_header = _normalize_gigachat_basic_auth(self.api_key or "")
            if not auth_header:
                raise RuntimeError("Missing GigaChat authorization key (Basic)")

            async with session.post(
                cfg.gigachat_oauth_url.strip(),
                headers={
                    **request_id_headers(),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "RqUID": str(uuid.uuid4()),
                    "Authorization": auth_header,
                },
                data={"scope": cfg.gigachat_scope.strip()},
                ssl=cfg.gigachat_verify_ssl_certs,
                timeout=aiohttp.ClientTimeout(total=cfg.gigachat_oauth_timeout_seconds),
            ) as resp:
                raw_text = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(
                        f"GigaChat token request failed {resp.status}: "
                        f"{raw_text.strip() or 'empty response'}"
                    )
                payload = await resp.json(content_type=None)

            if not isinstance(payload, dict):
                raise RuntimeError("GigaChat token response must be a JSON object")
            access_token = (
                payload.get("access_token")
                or payload.get("accessToken")
                or payload.get("token")
            )
            if not isinstance(access_token, str) or not access_token.strip():
                raise RuntimeError(
                    "GigaChat token response did not include access_token"
                )

            _gigachat_token_cache = _GigaChatToken(
                access_token=access_token.strip(),
                expires_at=_parse_gigachat_expires_at(payload, now=time.time()),
            )
            return _gigachat_token_cache.access_token

    @property
    def chat_suggestion_timeout_seconds(self) -> float:
        return cfg.gigachat_suggest_timeout_seconds

    @property
    def chat_completion_response_format(self) -> dict[str, Any]:
        return {"type": "json_object"}

    @property
    def chat_completion_format_instruction(self) -> str:
        return 'Верни только JSON-объект вида {"actions": ["..."]}.'


PROVIDER_CLASSES: tuple[type[BaseAIProvider], ...] = (
    GigaChatProvider,
    OpenAIProvider,
    YandexGPTProvider,
)


def _iter_providers() -> Iterable[BaseAIProvider]:
    for cls in PROVIDER_CLASSES:
        yield cls()


def list_ai_providers() -> list[BaseAIProvider]:
    return list(_iter_providers())


def get_ai_provider_options() -> list[dict[str, Any]]:
    return [provider.to_dict() for provider in list_ai_providers()]


def get_provider(provider_id: str) -> BaseAIProvider:
    for provider in _iter_providers():
        if provider.id == provider_id:
            return provider
    raise ValueError(f"Unknown AI provider '{provider_id}'")


def get_default_provider_id() -> str:
    providers = list_ai_providers()
    if not providers:
        raise RuntimeError("No AI providers configured")
    return providers[0].id


def resolve_ai_settings(
    provider_id: str,
    model_id: str,
) -> tuple[BaseAIProvider, ModelInfo]:
    provider = get_provider(provider_id)
    model = provider.get_model(model_id)
    return provider, model


DEFAULT_OPENAI_MODEL = OpenAIProvider._models[0].id
