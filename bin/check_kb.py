from __future__ import annotations

import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KB_ROOT = PROJECT_ROOT / "kb"
INDEX_PATH = KB_ROOT / "index.md"
MAX_LINES = 220
LINK_RE = re.compile(r"\((kb/[^)#]+\.md)(?:#[^)]+)?\)|`(kb/[^`]+\.md)`")


def fail(message: str) -> None:
    print(f"kb check failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def markdown_files() -> list[Path]:
    if not KB_ROOT.exists():
        fail("kb/ directory is missing")
    return sorted(KB_ROOT.rglob("*.md"))


def check_index_exists() -> None:
    if not INDEX_PATH.exists():
        fail("kb/index.md is missing")


def check_file_sizes(files: list[Path]) -> None:
    oversized = []
    for path in files:
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count > MAX_LINES:
            oversized.append(f"{path.relative_to(PROJECT_ROOT)} ({line_count} lines)")
    if oversized:
        fail(
            "KB files exceed "
            f"{MAX_LINES} lines and should be split: {', '.join(oversized)}"
        )


def check_index_links() -> None:
    text = INDEX_PATH.read_text(encoding="utf-8")
    links = {
        match.group(1) or match.group(2)
        for match in LINK_RE.finditer(text)
    }
    missing = [
        link
        for link in sorted(links)
        if not (PROJECT_ROOT / link).exists()
    ]
    if missing:
        fail(f"kb/index.md links to missing files: {', '.join(missing)}")


def check_docs_policy() -> None:
    text = INDEX_PATH.read_text(encoding="utf-8").lower()
    required = "do not read `docs/`"
    if required not in text:
        fail("kb/index.md must state that docs/ is not agent operating knowledge")


def main() -> int:
    check_index_exists()
    files = markdown_files()
    check_file_sizes(files)
    check_index_links()
    check_docs_policy()
    print(f"kb check passed: {len(files)} markdown files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
