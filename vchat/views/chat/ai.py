from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Iterable

import tiktoken

from vchat.settings import config


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
        return config.get("openai_api_key")

    @property
    def base_url(self) -> str:
        return config.get("openai_base_url", "https://api.openai.com/v1")


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
        return config.get("yandex_api_key")

    @property
    def base_url(self) -> str | None:
        return config.get("yandex_base_url")


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
        raw = config.get("gigachat_models")
        if isinstance(raw, list) and raw:
            items: list[ModelInfo] = []
            for entry in raw:
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
        return config.get("gigachat_api_key")

    @property
    def base_url(self) -> str | None:
        return config.get(
            "gigachat_base_url", "https://gigachat.devices.sberbank.ru/api/v1"
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
