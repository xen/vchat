from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import unquote, urlparse

_SEARCH_SEPARATOR_RE = re.compile(r"[^\w]+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_uri_slug(uri: str | None) -> str:
    if not uri:
        return ""

    parsed = urlparse(uri)
    if parsed.scheme or parsed.netloc:
        value = " ".join(part for part in (parsed.netloc, parsed.path, parsed.query) if part)
    else:
        value = uri
    value = unquote(value).lower()
    value = _SEARCH_SEPARATOR_RE.sub(" ", value)
    return _WHITESPACE_RE.sub(" ", value).strip()


def entity_terms_to_text(entity_terms: Sequence[str] | None) -> str:
    if not entity_terms:
        return ""
    return " ".join(term.strip() for term in entity_terms if term and term.strip())
