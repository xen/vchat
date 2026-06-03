from __future__ import annotations

from urllib.parse import urlparse


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
