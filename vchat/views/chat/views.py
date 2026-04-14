import logging
import re
import time
from dataclasses import dataclass
from typing import Any, List, Optional
import aiohttp_jinja2

import aiohttp
import redis.asyncio as aioredis
import sqlalchemy as sa
from aiohttp import web
from guardrails import GuardrailTripwireTriggered
from itsdangerous import (
    BadSignature,
    URLSafeSerializer,
)

from vchat.ai_providers import (
    BaseAIProvider,
    ModelInfo,
    get_default_provider_id,
    resolve_ai_settings,
)
from vchat.db import async_session_factory
from vchat.guardrails import (
    check_input_guardrails,
    check_output_guardrails,
    extract_tripwire_details,
    get_guardrails_client,
)
from vchat.metrics import record_chat_request
from vchat.models import Chat, ChatMsg
from vchat.project_settings import get_setting, get_setting_json
from vchat.settings import config
from vchat.utils import json, run_task

from .ctx import chat_id_ctx, get_context, user_id_ctx

# Regex for detecting trivial/greeting messages to skip RAG
TRIVIAL_PATTERNS = [
    r"^(hi|hello|hey|hola|bonjour|privet|test|ping)$",
    r"^(hi|hello|hey)\s+(there|bot|ai|assistant)$",
    r"^\?+$",
]
TRIVIAL_REGEX = re.compile(r"|".join(TRIVIAL_PATTERNS), re.IGNORECASE)


def is_trivial_query(text: str) -> bool:
    """Check if the query is a simple greeting or test compatible with skipping RAG."""
    return bool(TRIVIAL_REGEX.match(text.strip()))


def extract_total_tokens(usage_data: Any) -> int:
    """Normalize token usage payload from different provider/client formats."""
    if not usage_data:
        return 0
    if hasattr(usage_data, "model_dump"):
        usage_data = usage_data.model_dump()
    if not isinstance(usage_data, dict):
        return 0

    def _as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    total = _as_int(usage_data.get("total_tokens"))
    if total > 0:
        return total

    total = _as_int(usage_data.get("total"))
    if total > 0:
        return total

    prompt = _as_int(usage_data.get("prompt_tokens") or usage_data.get("input_tokens"))
    completion = _as_int(
        usage_data.get("completion_tokens") or usage_data.get("output_tokens")
    )
    if prompt > 0 or completion > 0:
        return prompt + completion

    nested = usage_data.get("usage")
    if isinstance(nested, dict):
        return extract_total_tokens(nested)
    return 0


@dataclass
class GenerationContext:
    provider: BaseAIProvider
    model: ModelInfo
    system_prompt: str
    topics: list[str]
    intents: list[str]

    @property
    def provider_id(self) -> str:
        return self.provider.id

    @property
    def model_id(self) -> str:
        return self.model.id

    def request_meta(self) -> dict[str, Any]:
        return self.provider.request_meta()


def build_generation_context(app) -> GenerationContext:
    provider_id = get_setting(app, "project.provider", get_default_provider_id())
    if not provider_id:
        provider_id = get_default_provider_id()
    model_id = get_setting(app, "project.model")
    provider, model = resolve_ai_settings(provider_id, model_id)
    system_prompt = get_setting(app, "project.system_prompt", SYSTEM_PROMPT) or SYSTEM_PROMPT
    topics = get_setting_json(app, "project.topics", [])
    intents = get_setting_json(app, "project.intents", [])
    if not isinstance(topics, list):
        topics = []
    if not isinstance(intents, list):
        intents = []
    return GenerationContext(provider, model, system_prompt, topics, intents)


async def generate_suggestions(
    messages: List[dict], ctx: GenerationContext
) -> List[str]:
    """
    Generate 3 short, relevant follow-up actions/questions for the user.
    Uses a lightweight call to a fast model.
    """
    provider_id = ctx.provider_id
    if provider_id != "openai":
        return []
    request_meta = ctx.request_meta()
    api_key = request_meta.get("api_key") or OPENAI_API_KEY
    base_url = request_meta.get("base_url") or OPENAI_BASE_URL
    model = ctx.model_id or OPENAI_MODEL
    if not api_key:
        return []

    # Construct a prompt for suggestions
    # We only need the last few messages to understand context
    recent_messages = messages[-4:]

    prompt = [
        {
            "role": "system",
            "content": (
                "You are a helpful assistant. Based on the conversation history, "
                "suggest 3 short, concise follow-up questions or actions the user might want to take next. "
                'Return ONLY a raw JSON array of strings, e.g. ["Action 1", "Action 2"]. '
                "Keep them relevant to the workspace context if clear. "
                "Do not return markdown formatting."
            ),
        },
    ]

    # Inject configured context if available
    if ctx.topics or ctx.intents:
        context_str = "Configured Context:\n"
        if ctx.topics:
            context_str += f"- Topics: {', '.join(ctx.topics)}\n"
        if ctx.intents:
            context_str += f"- Potential Intents: {', '.join(ctx.intents)}\n"

        prompt[0]["content"] += (
            "\n\n"
            + context_str
            + "Try to align suggestions with these topics/intents if relevant."
        )

    prompt.extend(recent_messages)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": prompt,
                    "max_tokens": 200,
                    "temperature": 0.5,
                },
                timeout=aiohttp.ClientTimeout(total=10.0),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data["choices"][0]["message"]["content"]
                    # Clean up potential markdown code blocks
                    content = content.strip()
                    if content.startswith("```"):
                        content = content.strip("`json \n")
                    try:
                        suggestions = json.loads(content)
                        if isinstance(suggestions, list):
                            return suggestions[:3]
                    except json.JSONDecodeError:
                        pass
    except Exception as e:
        logger.warning(f"Failed to generate suggestions: {e}")

    return []


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vchat.chat")

# Configuration from settings
OPENAI_API_KEY = config.get("openai_api_key")
OPENAI_BASE_URL = config.get("openai_base_url", "https://api.openai.com/v1")
OPENAI_MODEL = config.get("openai_model", "gpt-4o-mini")
REDIS_URL = config.get("redis_uri", "redis://localhost:6379/3")
SECRET_KEY = config.get("secret_key")
CELERY_DEFAULT_QUEUE = config.get("celery_default_queue", "embeddings")

# System prompt for chat
SYSTEM_PROMPT = """## Role and Objective
You are a friendly and helpful AI assistant.

## Instructions
- Always reply in the same language as the user's message.
- Carefully review the entire chat history before responding; ensure replies are fully
informed by previous context.
- Deliver complete and accurate information and real code or data—never use placeholders
or omit factual content.
- If a response reaches the character limit, stop and wait for the user to prompt with
"continue"; do not condense or leave information incomplete.
- Accuracy is paramount—incorrect answers may result in penalties.
- Never create or infer information not grounded in factual context.
- Do not miss or omit any essential or critical context; prioritize contextual relevance
in every response.
- Follow all Answering Rules below for every reply.
- Deliver thoughtful, well-considered responses; reason internally before replying. Do
not reveal internal reasoning.
- Use inline citations in the format [[citation:ID]] when referring to context.
- Do not list sources at the end; explicit source blocks are handled by the UI.

## Answering Rules (strict order):
1. Integrate expert-level knowledge and clear, stepwise reasoning to deliver accurate,
detailed answers with concrete examples and actionable details.
2. Approach every response as if it could lead to a significant reward—aim for excellence.
3. Treat every answer as critically important to the user's career or objectives.
4. Ensure a conversational, natural, and human-like tone for all responses.
"""

# Tools configuration - empty for now (tools.py removed)
TOOLS = []

GUARDRAIL_USER_MESSAGE = "Извините, я не могу дать корректный ответ на этот запрос."

# --- Redis (optional) support for background tasks ---
redis = aioredis.from_url(REDIS_URL, decode_responses=True)


def _is_guardrail_blocked(reasons: set[str]) -> bool:
    blocking_reasons = {
        "input_blocked",
        "output_blocked",
        "guardrail_tripwire",
    }
    return bool(reasons & blocking_reasons)


async def ai_chat_stream(messages: List[dict], ctx: GenerationContext):
    provider_meta = ctx.request_meta()
    provider_id = ctx.provider_id
    if provider_id != "openai":
        raise web.HTTPBadRequest(text=f"Provider '{provider_id}' is not supported yet")
    api_key = provider_meta.get("api_key") or OPENAI_API_KEY
    base_url = provider_meta.get("base_url") or OPENAI_BASE_URL
    if not api_key:
        raise web.HTTPBadRequest(text="Missing API key for selected provider")

    model = ctx.model_id or OPENAI_MODEL
    current_system_prompt = ctx.system_prompt or SYSTEM_PROMPT
    guardrails_client = get_guardrails_client(api_key=api_key, base_url=base_url)
    assistant_message = {"role": "assistant", "content": ""}
    pending_tool_calls: dict[int, dict] = {}

    if guardrails_client is not None:
        stream = await guardrails_client.chat.completions.create(
            messages=[
                {"role": "system", "content": current_system_prompt},
                *messages,
            ],
            model=model,
            temperature=0.2,
            stream=True,
            max_tokens=2048,
            stream_options={"include_usage": True},
        )
        async for guarded_chunk in stream:
            chunk = (
                getattr(guarded_chunk, "_llm_response", None)
                or getattr(guarded_chunk, "llm_response", None)
                or guarded_chunk
            )

            usage = getattr(chunk, "usage", None)
            if usage:
                usage_data = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
                yield {"event": "usage", "usage": usage_data}

            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue

            choice = choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason == "content_filter":
                yield {"event": "guardrail", "reason": "content_filter"}

            delta = getattr(choice, "delta", None)
            if not delta:
                continue

            role = getattr(delta, "role", None)
            if role:
                assistant_message["role"] = role

            content = getattr(delta, "content", None)
            if content:
                assistant_message["content"] += content
                yield {"event": "content", "data": content}

            refusal = getattr(delta, "refusal", None)
            if refusal:
                yield {"event": "guardrail", "reason": "refusal"}

            tool_calls_delta = getattr(delta, "tool_calls", None) or []
            for tool_call in tool_calls_delta:
                idx = getattr(tool_call, "index", 0) or 0
                existing = pending_tool_calls.setdefault(
                    idx,
                    {
                        "id": None,
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )

                tool_call_id = getattr(tool_call, "id", None)
                if tool_call_id:
                    existing["id"] = tool_call_id

                tool_type = getattr(tool_call, "type", None)
                if tool_type:
                    existing["type"] = tool_type

                func = getattr(tool_call, "function", None)
                if func:
                    func_name = getattr(func, "name", None)
                    if func_name:
                        existing["function"]["name"] = func_name

                    func_args = getattr(func, "arguments", None)
                    if func_args:
                        existing["function"]["arguments"] += func_args
    else:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": current_system_prompt},
                        *messages,
                    ],
                    "temperature": 0.2,
                    "stream": True,
                    "max_tokens": 2048,
                    "stream_options": {"include_usage": True},
                },
                timeout=aiohttp.ClientTimeout(total=60.0),
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

                async for line_bytes in resp.content:
                    line = line_bytes.decode("utf-8").strip()
                    if not line:
                        continue
                    if line.startswith("data:"):
                        data = line[len("data:") :].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)

                            # Handle usage if present (usually in the last chunk)
                            if "usage" in chunk and chunk["usage"]:
                                yield {"event": "usage", "usage": chunk["usage"]}
                                continue

                            choices = chunk.get("choices", [])
                            if not choices:
                                continue
                            choice = choices[0]
                            finish_reason = choice.get("finish_reason")
                            if finish_reason == "content_filter":
                                yield {
                                    "event": "guardrail",
                                    "reason": "content_filter",
                                }
                            delta = choice.get("delta", {})
                            if "role" in delta and delta["role"]:
                                assistant_message["role"] = delta["role"]

                            content = delta.get("content")
                            if content:
                                assistant_message["content"] += content
                                yield {"event": "content", "data": content}

                            refusal = delta.get("refusal")
                            if refusal:
                                yield {"event": "guardrail", "reason": "refusal"}

                            tool_calls_delta = delta.get("tool_calls") or []
                            for tool_call in tool_calls_delta:
                                idx = tool_call.get("index", 0)
                                existing = pending_tool_calls.setdefault(
                                    idx,
                                    {
                                        "id": None,
                                        "type": tool_call.get("type", "function"),
                                        "function": {"name": "", "arguments": ""},
                                    },
                                )
                                if tool_call.get("id"):
                                    existing["id"] = tool_call["id"]
                                if tool_call.get("type"):
                                    existing["type"] = tool_call["type"]

                                func = tool_call.get("function") or {}
                                if func.get("name"):
                                    existing["function"]["name"] = func["name"]
                                if func.get("arguments"):
                                    existing["function"]["arguments"] += func["arguments"]
                        except Exception:
                            continue

    tool_call_events = []
    if pending_tool_calls:
        for idx in sorted(pending_tool_calls.keys()):
            call = pending_tool_calls[idx]
            raw_args = (call.get("function") or {}).get("arguments", "")
            parsed_args = {}
            if raw_args:
                try:
                    parsed_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    try:
                        parsed_args = json.loads(raw_args.strip())
                    except json.JSONDecodeError:
                        parsed_args = {}
            tool_call_events.append(
                {
                    "event": "tool_call",
                    "id": call.get("id"),
                    "name": (call.get("function") or {}).get("name"),
                    "arguments": parsed_args,
                    "raw_arguments": raw_args,
                }
            )

        assistant_message["tool_calls"] = [
            {
                "id": call.get("id"),
                "type": call.get("type") or "function",
                "function": {
                    "name": (call.get("function") or {}).get("name"),
                    "arguments": (call.get("function") or {}).get(
                        "arguments", ""
                    ),
                },
            }
            for idx, call in sorted(pending_tool_calls.items())
        ]

    for event in tool_call_events:
        yield event

    assistant_message["content"] = assistant_message["content"] or None
    if not assistant_message.get("tool_calls"):
        assistant_message.pop("tool_calls", None)

    yield {"event": "assistant_message", "message": assistant_message}


async def websocket(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    serializer = URLSafeSerializer(SECRET_KEY)
    async def persist_guardrail_messages(
        *,
        user_text: str,
        assistant_text: str,
        reasons: set[str],
        stage: str,
    ) -> tuple[int, int]:
        marker = f"guardrail_blocked_{stage}|{','.join(sorted(reasons))}"
        async with async_session_factory() as db:
            res_user = await db.execute(
                sa.insert(ChatMsg)
                .values(
                    text=user_text,
                    role="user",
                    full_context=marker,
                    chat_id=chat_id_ctx.get(),
                    user_uid=str(user_id_ctx.get()),
                    created_at=sa.func.now(),
                    guardrail_triggered=False,
                    guardrail_stage=None,
                    guardrail_reasons=None,
                )
                .returning(ChatMsg.id)
            )
            user_msg_id = res_user.scalar_one()

            res_ai = await db.execute(
                sa.insert(ChatMsg)
                .values(
                    text=assistant_text,
                    role="assistant",
                    full_context=marker,
                    chat_id=chat_id_ctx.get(),
                    user_uid=str(user_id_ctx.get()),
                    created_at=sa.func.now(),
                    used_chunks=[],
                    tokens=0,
                    provider=assistant_provider,
                    model=assistant_model,
                    guardrail_triggered=True,
                    guardrail_stage=stage,
                    guardrail_reasons=sorted(reasons) or None,
                )
                .returning(ChatMsg.id)
            )
            assistant_msg_id = res_ai.scalar_one()
            await db.commit()

        return user_msg_id, assistant_msg_id

    try:
        payload = request.match_info.get("payload")
        user_id, chat_id = serializer.loads(payload, salt="vchat", max_age=3600)
        if not isinstance(chat_id, str) or not chat_id:
            await ws.close(code=1008)
            return ws

        async with async_session_factory() as db:
            exists = await db.scalar(sa.select(Chat.id).where(Chat.id == chat_id))
            if not exists:
                await ws.close(code=1008)
                return ws

        user_id_ctx.set(user_id)
        chat_id_ctx.set(chat_id)
    except (BadSignature, ValueError, TypeError):
        # Policy Violation https://www.rfc-editor.org/rfc/rfc6455#section-7.4.1
        await ws.close(code=1008)
        return ws

    try:
        gen_context = build_generation_context(request.app)
        await redis.sadd("active_chats", chat_id_ctx.get())

        while True:
            msg = await ws.receive()
            if msg.type == web.WSMsgType.ERROR:
                print("ws connection closed with exception %s" % ws.exception())
                break

            user_text = ""
            if msg.type == web.WSMsgType.TEXT:
                if msg.data.strip().lower() == "ping":
                    await ws.send_str("pong")
                    continue
                user_text = msg.data

            if not user_text:
                continue

            try:
                assistant_message: Optional[dict] = None
                pending_tool_results: List[dict] = []
                used_chunks: List[dict] = []
                messages: List[dict] = []
                guardrail_reasons: set[str] = set()
                request_status = "ok"
                total_tokens = 0

                gen_context = build_generation_context(request.app)
                assistant_provider = gen_context.provider_id
                assistant_model = gen_context.model_id

                input_guardrails = await check_input_guardrails(
                    text=user_text,
                    provider=gen_context.provider,
                )
                guardrail_reasons.update(input_guardrails.reasons)
                if not input_guardrails.allowed:
                    request_status = "guardrail_blocked_input"
                    _, assistant_msg_id = await persist_guardrail_messages(
                        user_text=user_text,
                        assistant_text=GUARDRAIL_USER_MESSAGE,
                        reasons=guardrail_reasons,
                        stage="input",
                    )
                    serializer = URLSafeSerializer(SECRET_KEY)
                    signed_msg_id = serializer.dumps(assistant_msg_id, salt="chat_msg")
                    await ws.send_json(
                        {
                            "ok": True,
                            "content": GUARDRAIL_USER_MESSAGE,
                            "partial": False,
                            "sources": [],
                            "msg_id": assistant_msg_id,
                            "signed_msg_id": signed_msg_id,
                            "guardrail": True,
                        }
                    )
                    continue

                await redis.publish(
                    f"chat_monitor:{chat_id_ctx.get()}",
                    json.dumps(
                        {
                            "role": "user",
                            "content": user_text,
                            "timestamp": time.time(),
                        }
                    ),
                )

                skip_rag = is_trivial_query(user_text)

                async with async_session_factory() as ctx_db:
                    history, used_chunks = await get_context(
                        db=ctx_db,
                        chat_id=chat_id_ctx.get(),
                        prompt=user_text,
                        vector_top_k=0 if skip_rag else 10,
                        ft_top_m=0 if skip_rag else 10,
                    )

                messages = [dict(m._asdict()) for m in history]

                total_content = ""

                while True:
                    async for event in ai_chat_stream(messages, gen_context):
                        event_type = event.get("event")
                        if event_type == "content":
                            delta = event.get("data", "")
                            if delta:
                                total_content += delta
                                await ws.send_json(
                                    {"ok": True, "content": delta, "partial": True}
                                )
                                # Publish assistant partial message
                                await redis.publish(
                                    f"chat_monitor:{chat_id_ctx.get()}",
                                    json.dumps(
                                        {
                                            "role": "assistant",
                                            "content": delta,
                                            "partial": True,
                                            "timestamp": time.time(),
                                        }
                                    ),
                                )

                        elif event_type == "usage":
                            usage_data = event.get("usage", {})
                            parsed_total_tokens = extract_total_tokens(usage_data)
                            if parsed_total_tokens > 0:
                                total_tokens = max(total_tokens, parsed_total_tokens)
                        elif event_type == "guardrail":
                            reason = (event.get("reason") or "unknown").strip().lower()
                            if reason:
                                guardrail_reasons.add(reason)

                        elif event_type == "assistant_message":
                            assistant_message = event.get("message")
                        # Tool calls are disabled for now
                        # Skip tool_call events since TOOLS list is empty

                    if assistant_message is None:
                        raise RuntimeError("Assistant response missing from stream")

                    messages.append(assistant_message)

                    if not pending_tool_results:
                        break

                    for tool_call in pending_tool_results:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.get("id"),
                                "name": tool_call.get("name"),
                                "content": tool_call.get("result"),
                            }
                        )

                output_guardrails = await check_output_guardrails(
                    text=total_content,
                    provider=gen_context.provider,
                )
                guardrail_reasons.update(output_guardrails.reasons)
                if not output_guardrails.allowed:
                    request_status = "guardrail_blocked_output"
                    _, assistant_msg_id = await persist_guardrail_messages(
                        user_text=user_text,
                        assistant_text=GUARDRAIL_USER_MESSAGE,
                        reasons=guardrail_reasons,
                        stage="output",
                    )
                    serializer = URLSafeSerializer(SECRET_KEY)
                    signed_msg_id = serializer.dumps(assistant_msg_id, salt="chat_msg")
                    await ws.send_json(
                        {
                            "ok": True,
                            "content": GUARDRAIL_USER_MESSAGE,
                            "partial": False,
                            "sources": [],
                            "msg_id": assistant_msg_id,
                            "signed_msg_id": signed_msg_id,
                            "guardrail": True,
                        }
                    )
                    continue

                # Generate suggestions if not a trivial query (or even if it is, maybe?)
                # Let's generate suggestions generally, but maybe meaningful ones.
                # If we skipped RAG, maybe we don't need deep suggestions, but "How does X work?" might be good.
                suggestions = await generate_suggestions(
                    messages + [assistant_message], gen_context
                )
                if suggestions:
                    await ws.send_json(
                        {
                            "type": "suggested_actions",
                            "actions": suggestions,
                        }
                    )

                # Save both messages after stream completes
                async with async_session_factory() as db:
                    res_user = await db.execute(
                        sa.insert(ChatMsg)
                        .values(
                            text=user_text,
                            role="user",
                            full_context="",
                            chat_id=chat_id_ctx.get(),
                            user_uid=str(user_id_ctx.get()),
                            created_at=sa.func.now(),
                            guardrail_triggered=False,
                            guardrail_stage=None,
                            guardrail_reasons=None,
                        )
                        .returning(ChatMsg.id)
                    )
                    user_msg_id = res_user.scalar_one()

                    res_ai = await db.execute(
                        sa.insert(ChatMsg)
                        .values(
                            text=total_content,
                            role="assistant",
                            full_context="",
                            chat_id=chat_id_ctx.get(),
                            user_uid=str(user_id_ctx.get()),
                            created_at=sa.func.now(),
                            used_chunks=used_chunks,
                            tokens=total_tokens,
                            provider=assistant_provider,
                            model=assistant_model,
                            guardrail_triggered=_is_guardrail_blocked(guardrail_reasons),
                            guardrail_stage=(
                                "stream" if _is_guardrail_blocked(guardrail_reasons) else None
                            ),
                            guardrail_reasons=(
                                sorted(guardrail_reasons) if guardrail_reasons else None
                            ),
                        )
                        .returning(ChatMsg.id)
                    )
                    assistant_msg_id = res_ai.scalar_one()

                    await db.commit()

                # Send completion signal with sources AND message ID
                # We need to send this AFTER commit to ensure ID exists
                serializer = URLSafeSerializer(SECRET_KEY)
                signed_msg_id = serializer.dumps(assistant_msg_id, salt="chat_msg")

                # Send sources logic here or reuse?
                # The previous `ws.send_json` at line 511 was sent BEFORE DB insert.
                # We should move it here OR send a separate "saved" event?
                # The frontend expects "ok": True to finish streaming state.
                # If we move it here, latency is slightly higher (DB insert).
                # But we need the ID.

                # Re-constructing sources logic briefly
                sources = []
                if used_chunks:
                    seen_urls = set()
                    for chunk in used_chunks:
                        uri = chunk.get("uri")
                        title = chunk.get("title")
                        if uri and uri not in seen_urls:
                            sources.append({"uri": uri, "title": title or uri})
                            seen_urls.add(uri)

                await ws.send_json(
                    {
                        "ok": True,
                        "content": "",
                        "partial": False,
                        "sources": sources,
                        "msg_id": assistant_msg_id,  # Plain ID (maybe useful for JS internal, but less secure for public actions)
                        "signed_msg_id": signed_msg_id,
                    }
                )

                # Enqueue Celery-compatible background tasks (shared Redis queue)
                try:
                    await run_task(
                        task="jobs.embedder.tasks.index_chat_message",
                        msg_id=user_msg_id,
                    )
                    await run_task(
                        task="jobs.embedder.tasks.index_chat_message",
                        msg_id=assistant_msg_id,
                    )
                except Exception as enq_exc:
                    logger.warning("run_task failed: %s", enq_exc)
            except GuardrailTripwireTriggered as e:
                stage, reason = extract_tripwire_details(e)
                request_status = (
                    "guardrail_blocked_output" if stage == "output" else "guardrail_blocked_input"
                )
                guardrail_reasons.add("guardrail_tripwire")
                guardrail_reasons.add(reason)
                guardrail_reasons.add("output_blocked" if stage == "output" else "input_blocked")
                _, assistant_msg_id = await persist_guardrail_messages(
                    user_text=user_text,
                    assistant_text=GUARDRAIL_USER_MESSAGE,
                    reasons=guardrail_reasons,
                    stage=stage,
                )
                serializer = URLSafeSerializer(SECRET_KEY)
                signed_msg_id = serializer.dumps(assistant_msg_id, salt="chat_msg")
                await ws.send_json(
                    {
                        "ok": True,
                        "content": GUARDRAIL_USER_MESSAGE,
                        "partial": False,
                        "sources": [],
                        "msg_id": assistant_msg_id,
                        "signed_msg_id": signed_msg_id,
                        "guardrail": True,
                    }
                )
            except aiohttp.ClientResponseError as e:
                request_status = "provider_http_error"
                if e.status in {400, 403, 422}:
                    guardrail_reasons.add("provider_block")
                await ws.send_json(
                    {
                        "ok": False,
                        "error": "openai_http_error",
                        "status": e.status,
                        "detail": e.message,
                    }
                )
            except Exception as e:
                request_status = "internal_error"
                await ws.send_json(
                    {"ok": False, "error": type(e).__name__, "detail": str(e)}
                )
            finally:
                try:
                    record_chat_request(
                        provider=assistant_provider,
                        model=assistant_model,
                        tokens=total_tokens,
                        status=request_status,
                        guardrail_reasons=guardrail_reasons,
                    )
                except Exception as metrics_exc:
                    logger.warning("Failed to record chat metrics: %s", metrics_exc)
    except Exception as e:
        logger.error(f"Websocket exception: {e}")
    finally:
        await ws.close()
        try:
            await redis.srem("active_chats", chat_id_ctx.get())
        except Exception as e:
            logger.error("Failed to remove chat from active_chats in Redis: %s", e)
    return ws


async def chat_actions(request):
    action = request.match_info.get("action")
    item_id = request.match_info.get("item_id")

    # CSRF Check for HTMX
    token = request.headers.get("X-CSRFToken")
    if not token:
        raise web.HTTPForbidden(text="Missing CSRF Token")

    # Verify secure ID
    try:
        serializer = URLSafeSerializer(SECRET_KEY)
        # item_id should be signed
        real_id = serializer.loads(item_id, salt="chat_msg", max_age=86400)
    except BadSignature:
        raise web.HTTPForbidden(text="Invalid Item ID")

    db = request["db"]

    if action == "vote":
        data = await request.post()
        # HTMX might send form data, checking keys
        # If using hx-vars or hx-vals, it might be in post.
        # But we want to support both json and form?
        # HTMX typically sends form data.
        # Let's assume simplest case: button sends vote value
        # But wait, HTMX buttons usually don't send value unless put in hidden input or hx-vals.
        # We can use hx-vals='{"vote": 1}'

        # Check if vote is in query or post
        vote_raw = (data.get("vote") or request.query.get("vote") or "").strip().lower()
        vote: bool | None
        if vote_raw in {"1", "up", "true", "like"}:
            vote = True
        elif vote_raw in {"-1", "down", "false", "dislike"}:
            vote = False
        elif vote_raw in {"0", "none", "null", ""}:
            vote = None
        else:
            raise web.HTTPBadRequest(text="Invalid vote value")

        msg = await db.scalar(sa.select(ChatMsg).where(ChatMsg.id == int(real_id)))
        if not msg:
            raise web.HTTPNotFound(text="Message not found")

        if msg.role != "assistant":
            raise web.HTTPBadRequest(text="Can only vote on assistant messages")

        msg.vote = vote

        await db.commit()

        # Return the updated partial HTML for the buttons
        # We need to construct the HTML to swap.
        # Ideally this should be a template snippet.
        # For simplicity, inline string or render a small template?
        # Inline string for now to match the JS version's simplicity,
        # but better to use a macro in the template.
        # Let's try to render a partial template or just return the HTML string.
        # Given we are in aiohttp_jinja2 env, we can render context.

        return aiohttp_jinja2.render_template(
            "chat/includes/vote_buttons.html",
            request,
            {
                "msg_id": item_id,  # Keep signed ID
                "vote": msg.vote,
            },
        )

    return web.HTTPBadRequest(text=f"Unknown action: {action}")
