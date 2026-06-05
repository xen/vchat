from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urldefrag, urlsplit

import aiohttp
import sqlalchemy as sa

from vchat.ai_providers import BaseAIProvider, ModelInfo, resolve_ai_settings
from vchat.gigachat_oauth import get_gigachat_access_token
from vchat.models import Page, Source
from vchat.models.source_config import CrawlerRule
from vchat.page_status import PageStatus
from vchat.project_settings import get_setting_json
from vchat.settings import config
from vchat.utils import json


DEFAULT_TRIGGER_TEMPLATES = [
    "Хотите узнать больше о {title}?",
    "Помочь разобраться с {title}?",
    "Есть вопросы по {title}?",
    "Показать главное про {title}?",
    "Обсудим детали страницы {title}?",
    "Найти важное в {title}?",
]

TRIGGER_DEFAULTS_SETTING = "triggers.default_templates"
TRIGGER_GENERATION_LIMIT = 10
TRIGGER_CONTENT_CHARS = 8000
TRIGGER_RULE_MAX_LENGTH = 256
DEFAULT_SOURCE_TRIGGER_PATTERN = "^/.*"
UNSUPPORTED_TRIGGER_REGEX_TOKENS = (
    "(?=",
    "(?!",
    "(?<=",
    "(?<!",
    "(?P=",
    "(?(",
)
BACKREFERENCE_RE = re.compile(r"\\[1-9]")
NESTED_REPEAT_RE = re.compile(r"\([^)]*[*+{][^)]*\)[*+{]")


class TriggerPatternError(ValueError):
    pass


def canonical_page_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    if not url:
        return ""
    url, _fragment = urldefrag(url)
    return url.rstrip("/") or url


def trigger_key(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


def trigger_prompt_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def load_default_trigger_templates(app) -> list[str]:
    items = get_setting_json(app, TRIGGER_DEFAULTS_SETTING, DEFAULT_TRIGGER_TEMPLATES)
    if not isinstance(items, list):
        return list(DEFAULT_TRIGGER_TEMPLATES)
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    return cleaned or list(DEFAULT_TRIGGER_TEMPLATES)


def render_default_triggers(templates: list[str], title: str) -> list[dict[str, Any]]:
    page_title = "этой странице"
    cleaned_title = " ".join((title or "").split()).strip()
    if cleaned_title:
        page_title = cleaned_title[:120]
    rendered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for template in templates:
        text = template.replace("{title}", page_title).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        rendered.append(
            {
                "key": trigger_key(text),
                "text": text,
                "source": "default",
            }
        )
    return rendered[:10]


async def find_page_by_url(db, raw_url: str) -> Page | None:
    url = canonical_page_url(raw_url)
    if not url:
        return None
    variants = {url, url.rstrip("/"), f"{url.rstrip('/')}/"}
    return await db.scalar(sa.select(Page).where(Page.uri.in_(variants)).limit(1))


def page_trigger_items(page: Page) -> list[dict[str, Any]]:
    if not page.has_triggers or not isinstance(page.triggers, list):
        return []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in page.triggers:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split()).strip()
        if not text:
            continue
        key = str(item.get("key") or trigger_key(text))
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "key": key,
                "text": text,
                "source": str(item.get("source") or "generated"),
                "generated_at": item.get("generated_at"),
            }
        )
    return items


def build_page_trigger_items(texts: list[str], *, source: str) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text in texts:
        cleaned = " ".join((text or "").split()).strip()
        if not cleaned:
            continue
        key = trigger_key(cleaned)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "key": key,
                "text": cleaned,
                "source": source,
                "generated_at": now if source == "generated" else None,
            }
        )
    return result


def trigger_rule_url_part(source_url: str, raw_url: str) -> str:
    source = urlsplit(canonical_page_url(source_url))
    target = urlsplit((raw_url or "").strip())
    if not target.netloc:
        return ""
    if source.netloc and target.netloc != source.netloc:
        return ""
    path = target.path or "/"
    if target.query:
        return f"{path}?{target.query}"
    return path


def trigger_rules_match_url(
    raw_url: str, rules: list[CrawlerRule], *, source_url: str = ""
) -> bool:
    url = canonical_page_url(raw_url)
    if not url:
        return False
    target_url = trigger_rule_url_part(source_url, raw_url) if source_url else url
    if not target_url:
        return False
    patterns = [rule.value for rule in rules if rule.type == "regex" and rule.value]
    if not patterns:
        return False
    return any(trigger_pattern_matches_url(target_url, pattern) for pattern in patterns)


def validate_trigger_pattern(pattern: str) -> None:
    value = (pattern or "").strip()
    if not value:
        return
    if len(value) > TRIGGER_RULE_MAX_LENGTH:
        raise TriggerPatternError(
            f"Regex is too long; max length is {TRIGGER_RULE_MAX_LENGTH}"
        )
    if any(token in value for token in UNSUPPORTED_TRIGGER_REGEX_TOKENS):
        raise TriggerPatternError(
            "Lookaround, named backreferences and conditional regex constructs are not supported"
        )
    if BACKREFERENCE_RE.search(value):
        raise TriggerPatternError("Backreferences are not supported")
    if NESTED_REPEAT_RE.search(value):
        raise TriggerPatternError("Nested repeating groups are not supported")
    re.compile(value)


def trigger_pattern_matches_url(raw_url: str, pattern: str) -> bool:
    validate_trigger_pattern(pattern)
    url = raw_url.strip() if raw_url.startswith("/") else canonical_page_url(raw_url)
    if not url or not pattern.strip():
        return False
    return bool(re.search(pattern.strip(), url))


def source_trigger_rules_match_url(source: Source, raw_url: str) -> bool:
    if not source.enable_triggers:
        return False
    return trigger_rules_match_url(
        raw_url, source.config.trigger_rules, source_url=source.uri
    )


async def apply_source_trigger_rules(db, source: Source) -> int:
    pages = list(
        (
            await db.execute(
                sa.select(Page)
                .where(Page.source_id == source.id)
                .where(Page.uri.is_not(None))
                .where(Page.status == PageStatus.ready)
                .where(Page.status_error.is_(None))
            )
        ).scalars()
    )
    updated = 0
    for page in pages:
        matched = source_trigger_rules_match_url(source, page.uri or "")
        if page.has_triggers != matched:
            page.has_triggers = matched
            updated += 1
    return updated


def parse_generated_trigger_texts(raw: str) -> list[str]:
    payload = raw.strip()
    if payload.startswith("```"):
        payload = payload.strip("` \n")
        if payload.startswith("json"):
            payload = payload[4:].strip()
    data = json.loads(payload)
    if isinstance(data, dict):
        data = data.get("triggers")
    if not isinstance(data, list):
        raise ValueError("Trigger generation response must be a JSON array")

    result: list[str] = []
    seen: set[str] = set()
    for item in data:
        text = " ".join(str(item).split()).strip()
        if not text or text in seen:
            continue
        words = text.split()
        if len(words) > 10:
            text = " ".join(words[:10])
        seen.add(text)
        result.append(text)
    return result[:TRIGGER_GENERATION_LIMIT]


def build_trigger_generation_messages(page: Page) -> list[dict[str, str]]:
    title = " ".join((page.title or "").split()).strip() or "Без названия"
    content = " ".join((page.content or "").split()).strip()
    if len(content) > TRIGGER_CONTENT_CHARS:
        content = content[:TRIGGER_CONTENT_CHARS]
    return [
        {
            "role": "system",
            "content": (
                "Ты генерируешь короткие приглашения для чат-виджета. "
                "Верни только JSON-массив строк без markdown. "
                "Каждая строка должна быть до 10 слов, естественной и релевантной странице."
            ),
        },
        {
            "role": "user",
            "content": (
                "Сделай 10 зазывающих приглашений начать диалог по содержимому страницы.\n\n"
                f"URL: {page.uri or ''}\n"
                f"Title: {title}\n"
                f"Content:\n{content}"
            ),
        },
    ]


async def generate_trigger_texts_for_page(app, page: Page) -> list[str]:
    provider_id = config.get("chat_provider")
    model_id = config.get("chat_model")
    provider, model = resolve_ai_settings(provider_id, model_id)
    raw = await request_trigger_generation(
        provider, model, build_trigger_generation_messages(page)
    )
    return parse_generated_trigger_texts(raw)


async def request_trigger_generation(
    provider: BaseAIProvider,
    model: ModelInfo,
    messages: list[dict[str, str]],
) -> str:
    if not getattr(provider, "supports_chat", True):
        raise RuntimeError(f"Provider '{provider.id}' does not support chat")
    meta = provider.request_meta()
    api_key = meta.get("api_key")
    base_url = meta.get("base_url")
    if provider.id == "openai":
        api_key = api_key or config.get("openai_api_key")
        base_url = base_url or config.get(
            "openai_base_url", "https://api.openai.com/v1"
        )
    if not api_key:
        raise RuntimeError("Missing API key for trigger generation")
    if not base_url:
        raise RuntimeError("Missing base URL for trigger generation")

    async with aiohttp.ClientSession() as session:
        timeout_seconds = 30.0
        ssl = True
        if provider.id == "gigachat":
            api_key = await get_gigachat_access_token(
                session,
                basic_auth_key=api_key,
                oauth_timeout_seconds=float(
                    config.get("gigachat_oauth_timeout_seconds", 15.0)
                ),
            )
            timeout_seconds = float(
                config.get("gigachat_request_timeout_seconds", 60.0)
            )
            ssl = bool(config.get("gigachat_verify_ssl_certs", True))

        async with session.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model.id,
                "messages": messages,
                "temperature": 0.4,
                "max_tokens": 600,
            },
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ssl=ssl,
        ) as resp:
            if resp.status >= 400:
                detail = await resp.text()
                raise RuntimeError(f"Trigger generation failed: {resp.status} {detail}")
            data = await resp.json()
    return str(data["choices"][0]["message"]["content"])
