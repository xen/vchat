from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urldefrag, urlsplit

import sqlalchemy as sa

from vchat.models import Page, Source
from vchat.models.source_config import CrawlerRule
from vchat.views.projects.page_status import PageStatus


DEFAULT_TRIGGER_TEMPLATES = [
    "Хотите узнать больше о {title}?",
    "Помочь разобраться с {title}?",
    "Есть вопросы по {title}?",
    "Показать главное про {title}?",
    "Обсудим детали страницы {title}?",
    "Найти важное в {title}?",
]

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


def load_trigger_templates(items: Any = None) -> list[str]:
    if not isinstance(items, list):
        return list(DEFAULT_TRIGGER_TEMPLATES)
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    return cleaned or list(DEFAULT_TRIGGER_TEMPLATES)


def render_triggers(templates: list[str], title: str) -> list[dict[str, Any]]:
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
