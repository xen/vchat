from vchat.triggers import (
    TriggerPatternError,
    canonical_page_url,
    parse_generated_trigger_texts,
    render_default_triggers,
    trigger_key,
    trigger_pattern_matches_url,
    trigger_prompt_hash,
    trigger_rules_match_url,
    validate_trigger_pattern,
)
from vchat.models.source_config import CrawlerRule


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
    rules = [CrawlerRule(type="regex", value=r"^https://example.com/docs/")]

    assert trigger_rules_match_url("https://example.com/docs/page#part", rules) is True
    assert trigger_rules_match_url("https://example.com/blog/page", rules) is False


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
            "https://www.example.com/product?id=42",
            r"^https://www\.example\.com/product\?id=[0-9]+$",
        )
        is True
    )
    assert (
        trigger_pattern_matches_url(
            "https://www.example.com/product?id=abc",
            r"^https://www\.example\.com/product\?id=[0-9]+$",
        )
        is False
    )
