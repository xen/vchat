from types import SimpleNamespace

import pytest

from vchat import triggers
from vchat.triggers import (
    TriggerPatternError,
    canonical_page_url,
    parse_generated_trigger_texts,
    render_default_triggers,
    trigger_key,
    trigger_pattern_matches_url,
    trigger_prompt_hash,
    trigger_rule_url_part,
    trigger_rules_match_url,
    request_trigger_generation,
    source_trigger_rules_match_url,
    validate_trigger_pattern,
)
from vchat.models.source_config import CrawlerRule, SourceConfig


def test_render_default_triggers_substitutes_title_and_dedupes():
    rows = render_default_triggers(
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


def test_parse_generated_trigger_texts_accepts_array_and_limits_words():
    rows = parse_generated_trigger_texts(
        '["Короткий вопрос?", "Это слишком длинный триггер из большого количества слов для проверки лимита"]'
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


@pytest.mark.asyncio
async def test_request_trigger_generation_passes_gigachat_ssl_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    provider = SimpleNamespace(
        id="gigachat",
        supports_chat=True,
        request_meta=lambda: {
            "api_key": "basic-key",
            "base_url": "https://gigachat.example.local/api/v1",
        },
    )
    model = SimpleNamespace(id="GigaChat-Pro")

    async def _token(*args, **kwargs):
        _ = args, kwargs
        return "access-token"

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return False

        async def json(self):
            return {"choices": [{"message": {"content": "[\"Триггер\"]"}}]}

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return False

        def post(self, *args, **kwargs):
            _ = args
            captured.update(kwargs)
            return _Resp()

    monkeypatch.setitem(triggers.config, "gigachat_verify_ssl_certs", False)
    monkeypatch.setattr(triggers, "get_gigachat_access_token", _token)
    monkeypatch.setattr(triggers.aiohttp, "ClientSession", lambda: _Session())

    raw = await request_trigger_generation(
        provider,
        model,
        [{"role": "user", "content": "x"}],
    )

    assert raw == "[\"Триггер\"]"
    assert captured["ssl"] is False
