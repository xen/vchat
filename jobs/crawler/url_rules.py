from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

URL_RULE_MAX_REGEX_LENGTH = 256
UNSUPPORTED_URL_RULE_REGEX_TOKENS = (
    "(?=",
    "(?!",
    "(?<=",
    "(?<!",
    "(?P=",
    "(?(",
)
BACKREFERENCE_RE = re.compile(r"\\[1-9]")
NESTED_REPEAT_RE = re.compile(r"\([^)]*[*+{][^)]*\)[*+{]")


def safe_compile_url_rule_pattern(value: str) -> re.Pattern[str] | None:
    pattern = (value or "").strip()
    if not pattern:
        return None
    if len(pattern) > URL_RULE_MAX_REGEX_LENGTH:
        return None
    if any(token in pattern for token in UNSUPPORTED_URL_RULE_REGEX_TOKENS):
        return None
    if BACKREFERENCE_RE.search(pattern) or NESTED_REPEAT_RE.search(pattern):
        return None
    try:
        return re.compile(pattern)
    except re.error:
        return None


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
        compiled = safe_compile_url_rule_pattern(value)
        if compiled is not None:
            patterns.append(compiled)
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
    scheme = "https" if split.netloc else split.scheme
    ignored = ignored_query_params(rules)
    query_items = [
        (key, value)
        for key, value in parse_qsl(split.query, keep_blank_values=True)
        if key not in ignored
    ]
    query = urlencode(query_items, doseq=True)
    return urlunsplit((scheme, split.netloc, split.path, query, ""))


def url_allowed_by_rules(url: str, rules: list[dict] | None = None) -> bool:
    patterns = allowed_url_patterns(rules)
    if not patterns:
        return True
    return any(pattern.search(url) for pattern in patterns)


def extract_hostname(url: str | None) -> str:
    raw_url = (url or "").strip()
    if not raw_url:
        return ""
    return (urlparse(raw_url).hostname or "").lower().rstrip(".")


def build_source_id_by_host(source_rows: list[tuple[int, str]]) -> dict[str, int]:
    source_id_by_host: dict[str, int] = {}
    for source_id, source_uri in source_rows:
        host = extract_hostname(source_uri)
        if host and host not in source_id_by_host:
            source_id_by_host[host] = source_id
    return source_id_by_host


def resolve_source_id_for_url(
    url: str | None,
    source_id_by_host: dict[str, int],
    *,
    fallback_source_id: int | None = None,
) -> int | None:
    host = extract_hostname(url)
    if host and host in source_id_by_host:
        return source_id_by_host[host]
    return fallback_source_id
