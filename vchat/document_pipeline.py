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

from vchat.document_types import guess_document_type

logger = logging.getLogger(__name__)

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


def build_structure_from_markdown(markdown: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
    if candidate:
        cleaned = html.unescape(candidate).strip()
        if cleaned:
            return cleaned[:512]
    for block in structure:
        if block.get("type") == "heading":
            value = (block.get("content") or "").strip()
            if value:
                return value[:512]
    return None


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
    for node in list(soup.find_all(True)):
        attrs = " ".join(
            [
                " ".join(node.get("class", [])),
                node.get("id", "") or "",
            ]
        ).lower()
        if not attrs:
            continue
        if any(hint in attrs for hint in BOILERPLATE_HINTS):
            node.decompose()
            removed += 1
    return removed


def _html_to_markdown_like(body: str) -> tuple[str, str | None, int]:
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
    return normalize_markdown("\n".join(lines)), title, boilerplate_removed


def _docling_markdown_from_source(source: str) -> tuple[str | None, str | None]:
    try:
        result = get_docling_converter().convert(source)
        markdown = result.document.export_to_markdown()
        title = None
        if hasattr(result.document, "name"):
            title = getattr(result.document, "name")
        return markdown or None, title
    except Exception:
        logger.exception("Docling extraction failed for %s", source)
        return None, None


def extract_url_document(source_url: str) -> tuple[str, str | None, dict[str, Any]]:
    doc_type = guess_document_type(source_url, "text/html")
    markdown, title = _docling_markdown_from_source(source_url)
    if markdown:
        return build_document_payload(
            content=markdown,
            title=title,
            extractor="docling",
            fallback_used=False,
            degraded_mode=False,
            content_type="text/html",
            doc_type=doc_type,
        )

    response = requests.get(source_url, timeout=20)
    response.raise_for_status()
    body = response.text
    content_type = response.headers.get("Content-Type")
    markdown, fallback_title, removed = _html_to_markdown_like(body)
    normalized, normalized_title, meta = build_document_payload(
        content=markdown,
        title=fallback_title,
        extractor="beautifulsoup",
        fallback_used=True,
        degraded_mode=False,
        content_type=content_type,
        doc_type=doc_type,
    )
    meta["extraction"]["boilerplate_removed_count"] = removed
    return normalized, normalized_title, meta


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


def extract_local_file_document(file_path: str) -> tuple[str, str | None, dict[str, Any]]:
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
