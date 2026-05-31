from __future__ import annotations

import html
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pypdf
import requests
from bs4 import BeautifulSoup
from docling.document_converter import DocumentConverter

from vchat.document_shingles import (
    find_repeated_shingles,
    remove_shingles,
    visualize_removed_blocks,
)
from vchat.document_types import guess_document_type

logger = logging.getLogger(__name__)

DEFAULT_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 vChat/1.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.6",
}

_converter: DocumentConverter | None = None

BOILERPLATE_TAGS = ("header", "footer", "nav", "aside")
BOILERPLATE_HINTS = (
    "cookie",
    "cookies",
    "banner",
    "menu",
    "navbar",
    "nav",
    "header",
    "footer",
    "sidebar",
    "breadcrumbs",
    "breadcrumb",
    "advert",
    "promo",
    "share",
    "social",
)
BOILERPLATE_ATTR_HINTS = BOILERPLATE_HINTS + (
    "modal",
    "popup",
    "overlay",
    "dialog",
    "search",
    "login",
    "signin",
    "signup",
    "register",
    "auth",
)
AUTH_HEADING_HINTS = {
    "авторизация",
    "войти",
    "вход",
    "зарегистрироваться",
    "регистрация",
    "восстановить пароль",
    "забыли пароль",
    "login",
    "sign in",
    "sign up",
    "register",
    "forgot password",
}
CODE_FENCE_RE = re.compile(r"^```(?P<lang>[a-zA-Z0-9_+-]*)\s*$")


def get_docling_converter() -> DocumentConverter:
    global _converter
    if _converter is None:
        _converter = DocumentConverter()
    return _converter


def normalize_markdown(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized: list[str] = []
    blank_count = 0
    in_code_block = False
    for raw_line in lines:
        line = raw_line.rstrip()
        if CODE_FENCE_RE.match(line.strip()):
            in_code_block = not in_code_block
            blank_count = 0
            normalized.append(line.strip() or "```")
            continue
        if in_code_block:
            normalized.append(line)
            continue
        if not line.strip():
            blank_count += 1
            if blank_count <= 1:
                normalized.append("")
            continue
        blank_count = 0
        normalized.append(line)
    return "\n".join(normalized).strip()


def _looks_like_table(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    line = lines[index].strip()
    separator = lines[index + 1].strip()
    if "|" not in line or "|" not in separator:
        return False
    cells = [cell.strip() for cell in separator.strip("|").split("|")]
    if not cells:
        return False
    valid = [cell for cell in cells if re.fullmatch(r":?-{3,}:?", cell)]
    return len(valid) >= max(1, len(cells) - 1)


def build_outline(structure: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outline: list[dict[str, Any]] = []
    for block in structure:
        if block.get("type") != "heading":
            continue
        outline.append(
            {
                "level": block.get("level", 1),
                "content": block.get("content", ""),
                "section_path": block.get("section_path"),
            }
        )
    return outline


def build_structure_from_markdown(
    markdown: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    lines = markdown.splitlines()
    blocks: list[dict[str, Any]] = []
    heading_stack: list[str] = []
    paragraph_lines: list[str] = []
    table_count = 0
    code_count = 0
    list_count = 0
    paragraph_count = 0
    heading_count = 0
    code_lang = ""
    code_lines: list[str] = []
    ordered_items: list[str] = []
    unordered_items: list[str] = []

    def current_section_path() -> str | None:
        if not heading_stack:
            return None
        return " / ".join(heading_stack)

    def flush_paragraph() -> None:
        nonlocal paragraph_lines, paragraph_count
        text = "\n".join(paragraph_lines).strip()
        paragraph_lines = []
        if not text:
            return
        blocks.append(
            {
                "type": "paragraph",
                "content": text,
                "section_path": current_section_path(),
            }
        )
        paragraph_count += 1

    def flush_list() -> None:
        nonlocal ordered_items, unordered_items, list_count
        if ordered_items:
            blocks.append(
                {
                    "type": "list",
                    "ordered": True,
                    "items": ordered_items[:],
                    "section_path": current_section_path(),
                }
            )
            ordered_items = []
            list_count += 1
        if unordered_items:
            blocks.append(
                {
                    "type": "list",
                    "ordered": False,
                    "items": unordered_items[:],
                    "section_path": current_section_path(),
                }
            )
            unordered_items = []
            list_count += 1

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        fence = CODE_FENCE_RE.match(stripped)
        if fence:
            flush_paragraph()
            flush_list()
            if code_lines:
                blocks.append(
                    {
                        "type": "code",
                        "language": code_lang or "",
                        "content": "\n".join(code_lines).strip(),
                        "section_path": current_section_path(),
                    }
                )
                code_count += 1
                code_lines = []
                code_lang = ""
            else:
                code_lang = fence.group("lang") or ""
            index += 1
            while index < len(lines):
                next_line = lines[index]
                if CODE_FENCE_RE.match(next_line.strip()):
                    break
                code_lines.append(next_line.rstrip())
                index += 1
            if code_lines:
                blocks.append(
                    {
                        "type": "code",
                        "language": code_lang or "",
                        "content": "\n".join(code_lines).strip(),
                        "section_path": current_section_path(),
                    }
                )
                code_count += 1
                code_lines = []
                code_lang = ""
            if index < len(lines) and CODE_FENCE_RE.match(lines[index].strip()):
                index += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            heading_stack = heading_stack[: level - 1]
            heading_stack.append(title)
            blocks.append(
                {
                    "type": "heading",
                    "level": level,
                    "content": title,
                    "section_path": " / ".join(heading_stack),
                }
            )
            heading_count += 1
            index += 1
            continue

        if _looks_like_table(lines, index):
            flush_paragraph()
            flush_list()
            table_lines = [lines[index].rstrip(), lines[index + 1].rstrip()]
            index += 2
            while index < len(lines) and "|" in lines[index]:
                table_lines.append(lines[index].rstrip())
                index += 1
            blocks.append(
                {
                    "type": "table",
                    "content": "\n".join(table_lines).strip(),
                    "caption": None,
                    "section_path": current_section_path(),
                }
            )
            table_count += 1
            continue

        ordered = re.match(r"^\d+\.\s+(.*)$", stripped)
        unordered = re.match(r"^[-*+]\s+(.*)$", stripped)
        if ordered:
            flush_paragraph()
            unordered_items = []
            ordered_items.append(ordered.group(1).strip())
            index += 1
            continue
        if unordered:
            flush_paragraph()
            ordered_items = []
            unordered_items.append(unordered.group(1).strip())
            index += 1
            continue

        if not stripped:
            flush_paragraph()
            flush_list()
            index += 1
            continue

        paragraph_lines.append(line.rstrip())
        index += 1

    flush_paragraph()
    flush_list()
    metadata = {
        "word_count": len(markdown.split()),
        "table_count": table_count,
        "heading_count": heading_count,
        "list_count": list_count,
        "paragraph_count": paragraph_count,
        "code_block_count": code_count,
    }
    return blocks, metadata


def _coerce_title(candidate: str | None, structure: list[dict[str, Any]]) -> str | None:
    normalized_candidate = normalize_title_candidate(candidate)
    headings: list[str] = []
    for block in structure:
        if block.get("type") != "heading":
            continue
        value = normalize_title_candidate(block.get("content"))
        if value:
            headings.append(value)

    if normalized_candidate:
        if any(
            heading.casefold() == normalized_candidate.casefold()
            for heading in headings
        ):
            return normalized_candidate
        if headings and headings[0].casefold() in AUTH_HEADING_HINTS:
            return normalized_candidate

    for value in headings:
        return value
    return normalized_candidate


def normalize_title_candidate(candidate: str | None) -> str | None:
    if not candidate:
        return None

    cleaned = html.unescape(str(candidate))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None

    lowered = cleaned.lower()
    bad_markers = (
        "fatal error",
        "traceback (most recent call last)",
        "exception:",
        "warning:",
        "<script",
    )
    if any(marker in lowered for marker in bad_markers):
        return None

    # Guard against malformed HTML where <title> may absorb page body.
    if len(cleaned) > 220 or len(cleaned.split()) > 30:
        return None

    # Reject URL slugs: all-lowercase ASCII with no spaces (e.g. Docling's document.name).
    # Mixed-case strings like "Python" or "HTML5" are legitimate titles and pass through.
    if len(cleaned) >= 4 and re.fullmatch(r"[a-z][a-z0-9\-_]*", cleaned):
        return None

    return cleaned[:512]


def build_document_payload(
    *,
    content: str,
    title: str | None,
    extractor: str,
    fallback_used: bool,
    degraded_mode: bool,
    content_type: str | None = None,
    doc_type: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> tuple[str, str | None, dict[str, Any]]:
    normalized = normalize_markdown(content)
    structure, structure_meta = build_structure_from_markdown(normalized)
    outline = build_outline(structure)
    meta: dict[str, Any] = {
        "structure": structure,
        "outline": outline,
        "extraction": {
            "extractor": extractor,
            "fallback_used": fallback_used,
            "degraded_mode": degraded_mode,
            "table_count": structure_meta["table_count"],
            "word_count": structure_meta["word_count"],
            "boilerplate_removed_count": 0,
            "reason": "plain_text_fallback" if degraded_mode else None,
        },
    }
    if content_type:
        meta["content_type"] = content_type
    if doc_type:
        meta["doc_type"] = doc_type
    if extra_meta:
        meta.update(extra_meta)
    normalized_title = _coerce_title(title, structure)
    return normalized, normalized_title, meta


def _strip_boilerplate(soup: BeautifulSoup) -> int:
    removed = 0
    for tag_name in BOILERPLATE_TAGS:
        for node in soup.find_all(tag_name):
            node.decompose()
            removed += 1
    for node in soup.find_all(["dialog", "template"]):
        node.decompose()
        removed += 1
    for form in list(soup.find_all("form")):
        if form.find("input", attrs={"type": re.compile(r"password", re.I)}):
            form.decompose()
            removed += 1
    for node in list(soup.find_all(True)):
        attrs_dict = getattr(node, "attrs", None) or {}
        attrs = " ".join(
            [
                " ".join(attrs_dict.get("class", [])),
                attrs_dict.get("id", "") or "",
                attrs_dict.get("role", "") or "",
                attrs_dict.get("aria-label", "") or "",
            ]
        ).lower()
        if not attrs:
            continue
        if any(hint in attrs for hint in BOILERPLATE_ATTR_HINTS):
            node.decompose()
            removed += 1
    return removed


def _html_to_markdown_like(
    body: str, all_docs_markdown: list[str] = None
) -> tuple[str, str | None, int, list[str]]:
    soup = BeautifulSoup(body, "html.parser")
    boilerplate_removed = _strip_boilerplate(soup)
    title = soup.title.get_text(" ", strip=True)[:512] if soup.title else None
    container = soup.body or soup
    lines: list[str] = []
    for element in container.find_all(
        [
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "p",
            "li",
            "pre",
            "code",
            "table",
        ]
    ):
        text = element.get_text("\n", strip=True)
        if not text:
            continue
        if re.fullmatch(r"h[1-6]", element.name):
            level = int(element.name[1])
            lines.append(f"{'#' * level} {text}")
            lines.append("")
            continue
        if element.name == "li":
            prefix = "- "
            parent = element.parent.name if element.parent else ""
            if parent == "ol":
                siblings = [
                    sibling
                    for sibling in element.parent.find_all("li", recursive=False)
                ]
                index = siblings.index(element) + 1 if element in siblings else 1
                prefix = f"{index}. "
            lines.append(f"{prefix}{text}")
            continue
        if element.name == "table":
            rows = []
            for row in element.find_all("tr"):
                cells = row.find_all(["th", "td"])
                if not cells:
                    continue
                rows.append([cell.get_text(" ", strip=True) for cell in cells])
            if rows:
                header = rows[0]
                rows_md = [
                    f"| {' | '.join(header)} |",
                    f"| {' | '.join(['---'] * len(header))} |",
                ]
                rows_md.extend(f"| {' | '.join(row)} |" for row in rows[1:])
                lines.extend(rows_md)
                lines.append("")
            continue
        if element.name in {"pre", "code"}:
            lines.append("```")
            lines.append(text)
            lines.append("```")
            lines.append("")
            continue
        lines.append(text)
        lines.append("")
    markdown = normalize_markdown("\n".join(lines))
    removed_shingles = []
    if all_docs_markdown:
        shingles = find_repeated_shingles(
            all_docs_markdown + [markdown], k=10, min_freq=0.5
        )
        markdown, removed_shingles = remove_shingles(markdown, shingles)
    return markdown, title, boilerplate_removed, removed_shingles


def _docling_markdown_from_source(source: str) -> tuple[str | None, str | None]:
    try:
        result = get_docling_converter().convert(source)
        markdown = result.document.export_to_markdown()
        # document.name is the URL slug or filename — not a human-readable title.
        # For URLs we return None so _coerce_title picks the first heading instead.
        # For local files the caller supplies path.name as fallback.
        return markdown or None, None
    except Exception:
        logger.exception("Docling extraction failed for %s", source)
        return None, None


def _extract_nav_title(soup: BeautifulSoup, url: str) -> str | None:
    """
    Find a self-referencing navigation link on the page and return its text.

    Many sites (especially SPAs) share a generic <title> across all pages
    but expose human-readable page names in navigation anchors that link back
    to the current URL path.  The first such anchor whose text passes
    normalize_title_candidate is used as the page-specific title.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if not path:
        return None

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").rstrip("/")
        if href == path or href == url.rstrip("/"):
            text = a.get_text(" ", strip=True)
            candidate = normalize_title_candidate(text)
            if candidate:
                return candidate
    return None


def extract_url_document(source_url: str) -> tuple[str, str | None, dict[str, Any]]:
    doc_type = guess_document_type(source_url, "text/html")

    # Fetch the page once to get the HTML <title> tag (the authoritative page title)
    # and keep the body for the BeautifulSoup fallback if Docling fails.
    response = requests.get(source_url, timeout=20, headers=DEFAULT_HTTP_HEADERS)
    response.raise_for_status()
    body = response.text
    content_type = response.headers.get("Content-Type")
    soup = BeautifulSoup(body, "html.parser")
    html_title = soup.title.get_text(" ", strip=True)[:512] if soup.title else None

    # Prefer a self-referencing nav link over the generic site-wide <title>.
    nav_title = _extract_nav_title(soup, source_url)
    best_title = nav_title or html_title

    # TODO: собрать all_docs_markdown для шинглового анализа (эвристика)
    all_docs_markdown = []  # Для MVP: пусто, внедрить сборку при батч-обработке
    markdown, _, removed, removed_shingles = _html_to_markdown_like(
        body, all_docs_markdown=all_docs_markdown
    )
    if markdown.strip():
        normalized, normalized_title, meta = build_document_payload(
            content=markdown,
            title=best_title,
            extractor="beautifulsoup",
            fallback_used=False,
            degraded_mode=False,
            content_type=content_type,
            doc_type=doc_type,
        )
        meta["extraction"]["boilerplate_removed_count"] = removed
        if removed_shingles:
            meta["removed_shingles"] = visualize_removed_blocks(removed_shingles)
        return normalized, normalized_title, meta

    markdown, _ = _docling_markdown_from_source(source_url)
    if markdown:
        return build_document_payload(
            content=markdown,
            title=best_title,
            extractor="docling",
            fallback_used=True,
            degraded_mode=False,
            content_type="text/html",
            doc_type=doc_type,
        )

    return build_document_payload(
        content=body,
        title=best_title,
        extractor="plain_text_html",
        fallback_used=True,
        degraded_mode=True,
        content_type=content_type,
        doc_type=doc_type,
    )


def extract_pdf_text(file_path: str) -> str:
    reader = pypdf.PdfReader(file_path)
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n\n".join(parts).strip()


def extract_docx_text(file_path: str) -> str:
    try:
        import docx
    except ImportError:
        logger.exception("python-docx is not installed")
        return ""
    doc = docx.Document(file_path)
    paragraphs = [para.text for para in doc.paragraphs if para.text.strip()]
    return "\n\n".join(paragraphs).strip()


def extract_local_file_document(
    file_path: str,
) -> tuple[str, str | None, dict[str, Any]]:
    path = Path(file_path)
    suffix = path.suffix.lower()
    doc_type = guess_document_type(file_path, None)

    markdown, title = _docling_markdown_from_source(file_path)
    if markdown:
        return build_document_payload(
            content=markdown,
            title=title or path.name,
            extractor="docling",
            fallback_used=False,
            degraded_mode=False,
            doc_type=doc_type,
        )

    if suffix in {".txt", ".md", ".rtf"}:
        content = path.read_text(encoding="utf-8", errors="ignore")
    elif suffix == ".pdf":
        content = extract_pdf_text(file_path)
    elif suffix == ".docx":
        content = extract_docx_text(file_path)
    else:
        raise ValueError(f"Unsupported file type: {suffix}")

    normalized, normalized_title, meta = build_document_payload(
        content=content,
        title=path.name,
        extractor="plain_text",
        fallback_used=True,
        degraded_mode=True,
        doc_type=doc_type,
    )
    return normalized, normalized_title, meta


def summarize_structure(structure: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(block.get("type", "unknown") for block in structure)
    return dict(counts)
