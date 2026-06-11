from __future__ import annotations

from vchat.views.chat import views as chat_views


def test_system_prompt_requires_negative_grounded_answer_policy() -> None:
    prompt = chat_views.SYSTEM_PROMPT.casefold()

    assert "indexed context does not contain the requested answer" in prompt
    assert "not found in" in prompt
    assert "indexed sources" in prompt
    assert "do not guess" in prompt
    assert "do not cite unrelated context" in prompt


def test_system_prompt_requires_context_citation_ids() -> None:
    prompt = chat_views.SYSTEM_PROMPT.casefold()

    assert "use only citation ids" in prompt
    assert "provided context snippets" in prompt
    assert "never invent" in prompt
    assert "citation ids" in prompt


def test_system_prompt_prefers_compact_source_forward_answers() -> None:
    prompt = chat_views.SYSTEM_PROMPT.casefold()

    assert "отвечай кратко" in prompt
    assert "не возвращай markdown-таблицы" in prompt
    assert "не вставляй большие фрагменты исходного текста" in prompt
    assert "не включай блоки кода" in prompt
    assert "открыть источники" in prompt
