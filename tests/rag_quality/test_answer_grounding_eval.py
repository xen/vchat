from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.rag_quality import answer_eval
from vchat.views.chat import views as chat_views


def _cases() -> list[dict]:
    return answer_eval.load_cases()


class _StreamProvider:
    id = "openai"
    supports_chat = True
    chat_completion_verify_ssl_certs = True

    @property
    def base_url(self) -> str:
        return "https://example.invalid/v1"

    def request_meta(self) -> dict[str, str]:
        return {
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
        }

    async def chat_completion_bearer_token(self, session: object) -> str:
        del session
        return "test-key"


class _StreamModel:
    id = "rag-quality-stream-eval"


def _stream_chunk(content: str, *, role: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                finish_reason=None,
                delta=SimpleNamespace(
                    role=role,
                    content=content,
                    refusal=None,
                    tool_calls=[],
                ),
            )
        ],
    )


def test_answer_grounding_eval_fixture_covers_required_case_types() -> None:
    cases = _cases()
    present_types = {case["case_type"] for case in cases}

    assert answer_eval.REQUIRED_CASE_TYPES <= present_types


@pytest.mark.parametrize("case", _cases(), ids=lambda item: item["name"])
def test_answer_grounding_eval(case: dict) -> None:
    answer_eval.assert_case_schema(case)
    answer_eval.assert_grounded_answer(case, case["answer"])


@pytest.mark.parametrize("case", _cases(), ids=lambda item: item["name"])
def test_answer_grounding_context_pipeline_eval(case: dict) -> None:
    answer_eval.assert_case_schema(case)

    payload = answer_eval.context_payload(case)
    snippets = payload["snippets"]
    context_sources_by_id = {
        int(snippet["citation_id"]): snippet for snippet in snippets
    }
    cited_ids = {
        int(match.group(1))
        for match in answer_eval.CITATION_RE.finditer(case["answer"])
    }
    unknown_ids = cited_ids - context_sources_by_id.keys()
    context_urls = {snippet["uri"] for snippet in snippets}
    cited_context_urls = {
        context_sources_by_id[citation_id]["uri"] for citation_id in cited_ids
    }

    assert not unknown_ids, f"Citations missing from context: {sorted(unknown_ids)}"
    assert set(case["expected_source_urls"]) <= context_urls

    if case["citation_required"]:
        assert set(case["expected_source_urls"]) <= cited_context_urls

    expected_noise_urls = set(case.get("expected_context_noise_urls", []))
    assert expected_noise_urls <= context_urls
    assert not expected_noise_urls.intersection(cited_context_urls)


@pytest.mark.parametrize("case", _cases(), ids=lambda item: item["name"])
def test_captured_answer_generation_envelope_eval(case: dict) -> None:
    answer_eval.assert_case_schema(case)

    messages = answer_eval.generation_messages(case)
    assert messages[0]["role"] == "system"
    assert "Use inline citations in the format [[citation:ID]]" in messages[0][
        "content"
    ]
    assert "Use only citation IDs that appear" in messages[0]["content"]
    assert "not found in" in messages[0]["content"].casefold()
    assert messages[1]["role"] == "developer"
    assert messages[1]["content"].startswith("[context]\n")
    assert messages[2] == {"role": "user", "content": case["user_query"]}

    context_payload = json.loads(messages[1]["content"].split("\n", 1)[1])
    context_sources_by_id = {
        int(snippet["citation_id"]): snippet for snippet in context_payload["snippets"]
    }
    cited_ids = {
        int(match.group(1))
        for match in answer_eval.CITATION_RE.finditer(case["answer"])
    }
    cited_context_urls = {
        context_sources_by_id[citation_id]["uri"] for citation_id in cited_ids
    }

    if case["citation_required"]:
        assert set(case["expected_source_urls"]) <= cited_context_urls

    if case.get("negative_answer_required"):
        assert not cited_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _cases(), ids=lambda item: item["name"])
async def test_fake_streamed_answer_eval(
    case: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer_eval.assert_case_schema(case)

    answer = case["answer"]
    split_at = max(1, len(answer) // 2)
    stream_chunks = [
        _stream_chunk(answer[:split_at], role="assistant"),
        _stream_chunk(answer[split_at:]),
    ]
    captured_request: dict = {}

    async def _gen():
        for chunk in stream_chunks:
            yield chunk

    class _Completions:
        async def create(self, **kwargs):
            captured_request.update(kwargs)
            return _gen()

    guardrails_client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions())
    )
    monkeypatch.setattr(
        chat_views,
        "get_guardrails_client",
        lambda **_kwargs: guardrails_client,
    )

    events = [
        event
        async for event in chat_views.ai_chat_stream(
            answer_eval.context_and_user_messages(case),
            chat_views.GenerationContext(
                provider=_StreamProvider(),
                model=_StreamModel(),
                system_prompt=chat_views.SYSTEM_PROMPT,
            ),
        )
    ]
    content = "".join(
        event["data"] for event in events if event.get("event") == "content"
    )
    assistant_message = events[-1]["message"]

    assert captured_request["messages"] == chat_views.build_chat_completion_messages(
        chat_views.SYSTEM_PROMPT,
        answer_eval.context_and_user_messages(case),
    )
    assert all(
        message["role"] != "developer" for message in captured_request["messages"]
    )
    assert content == answer
    assert assistant_message["content"] == answer
    answer_eval.assert_grounded_answer(case, assistant_message["content"])
