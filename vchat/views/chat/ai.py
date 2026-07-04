from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Iterable

import requests
import tiktoken

from vchat.settings import cfg
from vchat.tracing import request_id_headers
from vchat.views.chat.oauth import get_gigachat_access_token_sync


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    context_window: int
    max_tokens: int
    tokenizer_name: str | None = None


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

    def get_model(self, model_id: str | None) -> ModelInfo:
        items = self.models
        if not items:
            raise ValueError(f"Provider '{self.id}' has no models configured")
        if not model_id:
            return items[0]
        for item in items:
            if item.id == model_id:
                return item
        return items[0]

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
            "GigaChat",
            "GigaChat",
            context_window=32768,
            max_tokens=4096,
            tokenizer_name="cl100k_base",
        ),
        ModelInfo(
            "GigaChat-Pro",
            "GigaChat Pro",
            context_window=32768,
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
                            context_window=32768,
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
        return get_gigachat_access_token_sync(
            basic_auth_key=self.api_key,
            oauth_timeout_seconds=cfg.gigachat_oauth_timeout_seconds,
        )


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
    model_id: str | None,
) -> tuple[BaseAIProvider, ModelInfo]:
    provider = get_provider(provider_id)
    model = provider.get_model(model_id)
    return provider, model


DEFAULT_OPENAI_MODEL = OpenAIProvider._models[0].id
