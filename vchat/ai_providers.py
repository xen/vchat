from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Iterable

from vchat.settings import config


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str


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


class OpenAIProvider(BaseAIProvider):
    id = "openai"
    title = "OpenAI"

    _models: ClassVar[list[ModelInfo]] = [
        ModelInfo("gpt-4o-mini", "GPT-4o mini"),
        ModelInfo("gpt-4o", "GPT-4o"),
        ModelInfo("gpt-3.5-turbo", "GPT-3.5 Turbo"),
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
        ModelInfo("yandexgpt-lite", "YandexGPT Lite"),
        ModelInfo("yandexgpt-pro", "YandexGPT Pro"),
    ]

    @property
    def models(self) -> list[ModelInfo]:
        return list(self._models)

    def _load_api_key(self) -> str | None:
        return config.get("yandex_api_key")


class GigaChatProvider(BaseAIProvider):
    id = "gigachat"
    title = "GigaChat"
    supports_chat = False

    _models: ClassVar[list[ModelInfo]] = [
        ModelInfo("gigachat-pro", "GigaChat Pro"),
    ]

    @property
    def models(self) -> list[ModelInfo]:
        return list(self._models)

    def _load_api_key(self) -> str | None:
        return config.get("gigachat_api_key")


PROVIDER_CLASSES: tuple[type[BaseAIProvider], ...] = (
    OpenAIProvider,
    YandexGPTProvider,
    GigaChatProvider,
)


def _iter_providers() -> Iterable[BaseAIProvider]:
    for cls in PROVIDER_CLASSES:
        yield cls()


def list_ai_providers(*, include_disabled: bool = False) -> list[BaseAIProvider]:
    # include_disabled kept for compatibility; all providers are returned
    return list(_iter_providers())


def get_ai_provider_options(*, include_disabled: bool = False) -> list[dict[str, Any]]:
    return [
        provider.to_dict()
        for provider in list_ai_providers(include_disabled=include_disabled)
    ]


def get_provider(provider_id: str) -> BaseAIProvider:
    for provider in _iter_providers():
        if provider.id == provider_id:
            return provider
    raise ValueError(f"Unknown AI provider '{provider_id}'")


def get_provider_choices(*, include_disabled: bool = False) -> list[tuple[str, str]]:
    return [
        (provider.id, provider.title)
        for provider in list_ai_providers(include_disabled=include_disabled)
    ]


def get_models_for_provider(provider_id: str) -> list[dict[str, str]]:
    provider = get_provider(provider_id)
    return [model.__dict__ for model in provider.models]


def get_model_choices(provider_id: str) -> list[tuple[str, str]]:
    provider = get_provider(provider_id)
    return [(model.id, model.label) for model in provider.models]


def get_default_provider_id() -> str:
    providers = list_ai_providers(include_disabled=False)
    if not providers:
        raise RuntimeError("No AI providers configured")
    return providers[0].id


def get_default_model_id(provider_id: str | None = None) -> str:
    target_id = provider_id or get_default_provider_id()
    provider = get_provider(target_id)
    return provider.models[0].id


def resolve_ai_settings(
    provider_id: str,
    model_id: str | None,
) -> tuple[BaseAIProvider, ModelInfo]:
    provider = get_provider(provider_id)
    model = provider.get_model(model_id)
    return provider, model


def is_provider_available(provider_id: str) -> bool:
    try:
        get_provider(provider_id)
        return True
    except ValueError:
        return False


def is_model_available(provider_id: str, model_id: str) -> bool:
    provider = get_provider(provider_id)
    return any(model.id == model_id for model in provider.models)


DEFAULT_OPENAI_MODEL = OpenAIProvider._models[0].id
