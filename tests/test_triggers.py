from types import SimpleNamespace

import pytest

from jobs.triggers import generation as trigger_generation
from jobs.triggers.generation import (
    generate_trigger_texts_for_page,
    parse_generated_trigger_texts,
    request_trigger_generation,
)
from vchat.views.chat import ai as chat_ai
from vchat.views.chat.ai import GigaChatProvider
from vchat.views.triggers.rules import (
    TriggerPatternError,
    canonical_page_url,
    render_triggers,
    trigger_key,
    trigger_pattern_matches_url,
    trigger_prompt_hash,
    trigger_rule_url_part,
    trigger_rules_match_url,
    source_trigger_rules_match_url,
    validate_trigger_pattern,
)
from vchat.models.source_config import CrawlerRule, SourceConfig


def test_render_triggers_substitutes_title_and_dedupes():
    rows = render_triggers(
        [
            "Хотите узнать о {title}?",
            "Хотите узнать о {title}?",
            "Разобраться с {title}",
        ],
        "  Тестовая   страница  ",
    )

    assert rows == [
        {
            "key": trigger_key("Хотите узнать о Тестовая страница?"),
            "text": "Хотите узнать о Тестовая страница?",
            "source": "default",
        },
        {
            "key": trigger_key("Разобраться с Тестовая страница"),
            "text": "Разобраться с Тестовая страница",
            "source": "default",
        },
    ]


def test_canonical_page_url_drops_fragment_and_trailing_slash():
    assert (
        canonical_page_url("https://example.com/docs/#part")
        == "https://example.com/docs"
    )
    assert canonical_page_url("https://example.com/") == "https://example.com"


def test_parse_generated_trigger_texts_accepts_object_and_limits_words():
    rows = parse_generated_trigger_texts(
        '{"triggers": ["Короткий вопрос?", "Это слишком длинный триггер из большого количества слов для проверки лимита"]}'
    )

    assert rows == [
        "Короткий вопрос?",
        "Это слишком длинный триггер из большого количества слов для проверки",
    ]


def test_trigger_prompt_hash_is_stable():
    assert trigger_prompt_hash("hello") == trigger_prompt_hash("hello")
    assert trigger_prompt_hash("hello") != trigger_prompt_hash("Hello")


def test_trigger_rules_match_url_uses_source_regex_rules():
    rules = [CrawlerRule(type="regex", value=r"^/docs/")]

    assert (
        trigger_rules_match_url(
            "https://example.com/docs/page#part",
            rules,
            source_url="https://example.com/",
        )
        is True
    )
    assert (
        trigger_rules_match_url(
            "https://example.com/blog/page",
            rules,
            source_url="https://example.com/",
        )
        is False
    )


def test_trigger_rules_match_url_rejects_other_source_domains():
    rules = [CrawlerRule(type="regex", value=r"^/docs/")]

    assert (
        trigger_rules_match_url(
            "https://other.example.com/docs/page",
            rules,
            source_url="https://example.com/",
        )
        is False
    )


def test_source_trigger_rules_match_url_requires_enabled_source():
    class SourceStub:
        uri = "https://example.com/"

        def __init__(self, enabled):
            self.enable_triggers = enabled
            self.config = SourceConfig(
                trigger_rules=[CrawlerRule(type="regex", value=r"^/docs/")],
            )

    assert (
        source_trigger_rules_match_url(
            SourceStub(True), "https://example.com/docs/page"
        )
        is True
    )
    assert (
        source_trigger_rules_match_url(
            SourceStub(False), "https://example.com/docs/page"
        )
        is False
    )


def test_trigger_rule_url_part_uses_path_and_query_inside_source_domain():
    assert (
        trigger_rule_url_part(
            "https://www.example.com/",
            "https://www.example.com/product?id=42#buy",
        )
        == "/product?id=42"
    )
    assert (
        trigger_rule_url_part(
            "https://www.example.com/",
            "https://other.example.com/product?id=42",
        )
        == ""
    )


def test_validate_trigger_pattern_rejects_expensive_or_negative_constructs():
    for pattern in [r"^https://example\.com/(?!admin)", r"(a)\1", r"^(a+)+$"]:
        try:
            validate_trigger_pattern(pattern)
        except TriggerPatternError:
            continue
        raise AssertionError(f"Expected pattern to be rejected: {pattern}")


def test_trigger_pattern_matches_url_accepts_simple_numeric_product_rule():
    assert (
        trigger_pattern_matches_url(
            "/product?id=42",
            r"^/product\?id=[0-9]+$",
        )
        is True
    )
    assert (
        trigger_pattern_matches_url(
            "/product?id=abc",
            r"^/product\?id=[0-9]+$",
        )
        is False
    )


def test_request_trigger_generation_passes_gigachat_ssl_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = []
    monkeypatch.setattr(chat_ai.cfg, "gigachat_api_key", "basic-key")
    monkeypatch.setattr(
        chat_ai.cfg,
        "gigachat_base_url",
        "https://gigachat.example.local/api/v1",
    )
    provider = GigaChatProvider()
    model = SimpleNamespace(id="GigaChat-2-Pro")

    class _Resp:
        def __init__(self, payload, *, status_code=200, text=""):
            self._payload = payload
            self.status_code = status_code
            self.text = text

        def json(self):
            return self._payload

    def _post(url, **kwargs):
        captured.append((url, kwargs))
        if url.endswith("/oauth"):
            return _Resp({"access_token": "access-token", "expires_in": 60})
        return _Resp(
            {"choices": [{"message": {"content": '{"triggers": ["Триггер"]}'}}]}
        )

    monkeypatch.setattr(chat_ai.cfg, "gigachat_verify_ssl_certs", False)
    monkeypatch.setattr(chat_ai, "_gigachat_token_cache", None)
    monkeypatch.setattr(chat_ai.requests, "post", _post)

    raw = request_trigger_generation(
        provider,
        model,
        [{"role": "user", "content": "x"}],
    )

    assert raw == '{"triggers": ["Триггер"]}'
    assert captured[0][1]["verify"] is False
    assert captured[1][1]["verify"] is False
    assert captured[1][1]["json"]["response_format"] == {"type": "json_object"}
    assert captured[1][1]["json"]["model"] == "GigaChat-2-Pro"


def test_generate_trigger_texts_uses_aux_model_instead_of_chat_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setattr(trigger_generation.cfg, "chat_provider", "main-provider")
    monkeypatch.setattr(trigger_generation.cfg, "chat_model", "main-model")
    monkeypatch.setattr(trigger_generation.cfg, "chat_aux_provider", "aux-provider")
    monkeypatch.setattr(trigger_generation.cfg, "chat_aux_model", "aux-model")

    class _Provider:
        id = "gigachat"

        def structured_json_response_format(self, *, name, schema):
            _ = name, schema
            return {"type": "json_object"}

        def request_chat_completion_sync(
            self,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            response_format,
        ):
            _ = messages, temperature, max_tokens, response_format
            captured["model"] = model.id
            return '{"triggers": ["Узнать подробнее"]}'

    def _resolve(provider_id, model_id):
        captured["provider"] = provider_id
        return _Provider(), SimpleNamespace(id=model_id)

    monkeypatch.setattr(trigger_generation, "resolve_ai_settings", _resolve)

    page = SimpleNamespace(
        uri="https://example.test/page",
        title="Тестовая страница",
        content="Описание страницы",
    )

    assert generate_trigger_texts_for_page(page) == ["Узнать подробнее"]
    assert captured == {"provider": "aux-provider", "model": "aux-model"}
