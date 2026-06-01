from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def ignored_query_params(rules: list[dict] | None) -> set[str]:
    return {
        (rule.get("value") or "").strip()
        for rule in (rules or [])
        if rule.get("type") == "param" and (rule.get("value") or "").strip()
    }


def allowed_url_patterns(rules: list[dict] | None) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    for rule in rules or []:
        if rule.get("type") != "regex":
            continue
        value = (rule.get("value") or "").strip()
        if not value:
            continue
        try:
            patterns.append(re.compile(value))
        except re.error:
            continue
    return patterns


def normalize_url_for_queue(url: str, rules: list[dict] | None = None) -> str:
    """
    Normalize URL before saving it in Page/PageLink/crawl queue:
    - strip fragment
    - drop ignored query params from source rules
    """
    raw_url = (url or "").strip()
    if not raw_url:
        return ""

    split = urlsplit(raw_url)
    ignored = ignored_query_params(rules)
    query_items = [
        (key, value)
        for key, value in parse_qsl(split.query, keep_blank_values=True)
        if key not in ignored
    ]
    query = urlencode(query_items, doseq=True)
    return urlunsplit((split.scheme, split.netloc, split.path, query, ""))


def url_allowed_by_rules(url: str, rules: list[dict] | None = None) -> bool:
    patterns = allowed_url_patterns(rules)
    if not patterns:
        return True
    return any(pattern.search(url) for pattern in patterns)
