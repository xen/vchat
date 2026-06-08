from __future__ import annotations

import mimetypes
import os
from urllib.parse import urlparse

DEFAULT_DOCUMENT_TYPE = "other"

DOCUMENT_TYPE_INFO: dict[str, dict[str, tuple[str, ...] | str]] = {
    "html": {
        "label": "HTML document",
        "extensions": (".html", ".htm", ".xhtml", ".shtml"),
        "content_types": ("text/html", "application/xhtml+xml"),
    },
    "markdown": {
        "label": "Markdown document",
        "extensions": (".md", ".markdown", ".mdown", ".mkd"),
        "content_types": ("text/markdown",),
    },
    "office": {
        "label": "Office document",
        "extensions": (
            ".doc",
            ".docx",
            ".ppt",
            ".pptx",
            ".xls",
            ".xlsx",
            ".odt",
            ".odp",
            ".ods",
            ".rtf",
            ".pdf",
        ),
        "content_types": (
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.ms-powerpoint",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.oasis.opendocument.text",
            "application/vnd.oasis.opendocument.spreadsheet",
            "application/vnd.oasis.opendocument.presentation",
            "application/rtf",
            "application/pdf",
        ),
    },
    "audio": {
        "label": "Audio file",
        "extensions": (
            ".mp3",
            ".wav",
            ".ogg",
            ".oga",
            ".flac",
            ".aac",
            ".m4a",
            ".opus",
        ),
        "content_types": ("application/ogg",),
    },
    "video": {
        "label": "Video file",
        "extensions": (".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv"),
        "content_types": (
            "application/x-mpegurl",
            "application/vnd.apple.mpegurl",
        ),
    },
    "code": {
        "label": "Source code",
        "extensions": (
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".java",
            ".c",
            ".h",
            ".cpp",
            ".hpp",
            ".cs",
            ".go",
            ".rb",
            ".php",
            ".rs",
            ".swift",
            ".scala",
            ".kt",
            ".json",
            ".yaml",
            ".yml",
            ".xml",
            ".csv",
            ".ini",
            ".cfg",
            ".toml",
            ".sh",
            ".bat",
            ".ps1",
            ".sql",
        ),
        "content_types": (
            "application/javascript",
            "text/javascript",
            "text/x-javascript",
            "text/css",
            "application/json",
            "application/ld+json",
            "application/xml",
            "text/xml",
            "text/x-python",
            "text/x-java-source",
            "text/x-c",
            "text/x-csrc",
            "text/x-c++",
            "text/x-c++src",
            "text/x-go",
            "text/x-ruby",
            "text/x-php",
            "text/x-shellscript",
            "application/x-sh",
            "application/x-python-code",
            "text/x-sql",
            "application/sql",
        ),
    },
    "other": {
        "label": "Other",
        "extensions": (),
        "content_types": (),
    },
}

_EXTENSION_TO_TYPE: dict[str, str] = {}
_CONTENT_TYPE_TO_TYPE: dict[str, str] = {}

for _doc_type, _info in DOCUMENT_TYPE_INFO.items():
    for _ext in _info.get("extensions", ()):  # type: ignore[arg-type]
        _EXTENSION_TO_TYPE[_ext] = _doc_type
    for _ctype in _info.get("content_types", ()):  # type: ignore[arg-type]
        _CONTENT_TYPE_TO_TYPE[_ctype] = _doc_type

_CODE_CONTENT_SUFFIXES = (
    "x-python",
    "x-java-source",
    "x-go",
    "x-ruby",
    "x-php",
    "x-shellscript",
    "x-c",
    "x-csrc",
    "x-c++",
    "x-c++src",
    "x-cpp",
    "x-typescript",
    "x-sql",
    "json",
    "ld+json",
    "yaml",
    "yml",
    "xml",
    "csv",
    "javascript",
    "ecmascript",
    "typescript",
    "sass",
    "scss",
    "less",
    "x-sh",
)


__all__ = [
    "DOCUMENT_TYPE_INFO",
    "guess_document_type",
    "get_document_type_label",
]


def guess_document_type(uri: str | None = None, content_type: str | None = None) -> str:
    """Return a normalized document type based on URI extension and MIME hints."""

    extension = ""
    if uri:
        extension = _extract_extension(uri)
        if extension in _EXTENSION_TO_TYPE:
            return _EXTENSION_TO_TYPE[extension]
        if not content_type:
            guessed_content_type = mimetypes.guess_type(uri)[0]
            if guessed_content_type:
                content_type = guessed_content_type

    if content_type:
        normalized = content_type.split(";", 1)[0].strip().lower()
        if normalized in _CONTENT_TYPE_TO_TYPE:
            return _CONTENT_TYPE_TO_TYPE[normalized]

        if normalized.startswith("audio/"):
            return "audio"
        if normalized.startswith("video/"):
            return "video"
        if normalized.startswith("text/"):
            subtype = normalized.split("/", 1)[1]
            if subtype == "html":
                return "html"
            if subtype == "markdown":
                return "markdown"
            if subtype in {"css"} or any(
                subtype.endswith(suffix) for suffix in _CODE_CONTENT_SUFFIXES
            ):
                return "code"
        if normalized.startswith("application/"):
            subtype = normalized.split("/", 1)[1]
            if subtype.endswith(("+json", "+xml")):
                return "code"
            if subtype in {"xhtml+xml"}:
                return "html"

    if extension:
        base, secondary = os.path.splitext(extension)
        if secondary and secondary in _EXTENSION_TO_TYPE:
            return _EXTENSION_TO_TYPE[secondary]
        if base and base in _EXTENSION_TO_TYPE:
            return _EXTENSION_TO_TYPE[base]

    return DEFAULT_DOCUMENT_TYPE


def get_document_type_label(doc_type: str) -> str:
    """Return a human-readable label for a document type."""

    info = DOCUMENT_TYPE_INFO.get(doc_type)
    if info:
        return info["label"]  # type: ignore[index]
    if not doc_type:
        return DOCUMENT_TYPE_INFO[DEFAULT_DOCUMENT_TYPE]["label"]  # type: ignore[index]
    return doc_type.replace("_", " ").title()


def _extract_extension(uri: str) -> str:
    parsed = urlparse(uri)
    path = parsed.path or uri
    _, ext = os.path.splitext(path)
    return ext.lower()
