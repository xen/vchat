import re
from dataclasses import dataclass
from typing import Any, List

from vchat.document_shingles import is_boilerplate_block
from vchat.embedding_tokenizer import load_embedding_tokenizer
from vchat.settings import config

EMBEDDING_MAX_SEQ_LENGTH = config["embedding_max_seq_length"]
EMBEDDING_CHUNK_MAX_TOKENS = config["embedding_chunk_max_tokens"]
EMBEDDING_CHUNK_OVERLAP_TOKENS = config["embedding_chunk_overlap_tokens"]
EMBEDDING_CHUNK_MAX_CHARS = config["embedding_chunk_max_chars"]
EMBEDDING_BLOCK_MAX_CHARS = max(
    EMBEDDING_CHUNK_MAX_CHARS,
    int(config.get("embedding_block_max_chars", 48000) or 48000),
)
EMBEDDING_ENTITY_SCAN_MAX_CHARS = max(
    EMBEDDING_CHUNK_MAX_CHARS,
    int(config.get("embedding_entity_scan_max_chars", 24000) or 24000),
)
EMBEDDING_DOCUMENT_MAX_CHARS = max(
    1,
    int(config.get("embedding_document_max_chars", 1_000_000) or 1_000_000),
)


class EmbedderDocumentError(RuntimeError):
    def __init__(self, message: str, *, page_id: int | None = None):
        super().__init__(message)
        self.page_id = page_id


@dataclass(frozen=True)
class ChunkData:
    index: int
    start: int | None
    end: int | None
    text: str
    kind: str
    header_text: str | None = None
    section_path: str | None = None
    entity_terms: list[str] | None = None
    token_count: int = 0


def get_embed_tokenizer() -> Any:
    if not hasattr(get_embed_tokenizer, "_tokenizer"):
        get_embed_tokenizer._tokenizer = load_embedding_tokenizer()
    return get_embed_tokenizer._tokenizer


def count_token_ids(tokenizer: Any, text: str) -> int:
    return len(
        tokenizer(
            text,
            add_special_tokens=False,
            truncation=False,
            verbose=False,
        )["input_ids"]
    )


def chunk_text_word_window(
    text: str,
    *,
    max_tokens: int | None = None,
    overlap: int | None = None,
) -> List[ChunkData]:
    if max_tokens is None:
        max_tokens = EMBEDDING_CHUNK_MAX_TOKENS
    if overlap is None:
        overlap = EMBEDDING_CHUNK_OVERLAP_TOKENS

    tokenizer = get_embed_tokenizer()
    tokens = text.split()
    n = len(tokens)
    chunks: List[ChunkData] = []
    if n == 0:
        return chunks

    i = 0
    ix = 0
    while i < n:
        token = tokens[i]
        if len(token) > EMBEDDING_CHUNK_MAX_CHARS:
            token_ids = tokenizer(
                token,
                add_special_tokens=False,
                truncation=False,
                verbose=False,
            )["input_ids"]
            for id_start in range(0, len(token_ids), max_tokens):
                piece_ids = token_ids[id_start : id_start + max_tokens]
                piece = tokenizer.decode(
                    piece_ids,
                    skip_special_tokens=True,
                    clean_up_tokenization_spaces=False,
                ).strip()
                if piece:
                    chunks.append(
                        ChunkData(
                            index=ix,
                            start=i,
                            end=i + 1,
                            text=piece,
                            kind="text",
                            token_count=len(piece_ids),
                        )
                    )
                    ix += 1
            i += 1
            continue

        high = i
        char_len = 0
        while high < n:
            token_chars = len(tokens[high]) if high == i else len(tokens[high]) + 1
            if high > i and char_len + token_chars > EMBEDDING_CHUNK_MAX_CHARS:
                break
            char_len += token_chars
            high += 1

        if high == i:
            i += 1
            continue

        best_end: int | None = None
        best_token_len = 0
        low = i + 1
        right = high
        while low <= right:
            mid = (low + right) // 2
            candidate_text = " ".join(tokens[i:mid])
            candidate_token_len = count_token_ids(tokenizer, candidate_text)
            if candidate_token_len <= max_tokens:
                best_end = mid
                best_token_len = candidate_token_len
                low = mid + 1
            else:
                right = mid - 1

        if best_end is None:
            token_ids = tokenizer(
                token,
                add_special_tokens=False,
                truncation=False,
                verbose=False,
            )["input_ids"]
            for id_start in range(0, len(token_ids), max_tokens):
                piece_ids = token_ids[id_start : id_start + max_tokens]
                piece = tokenizer.decode(
                    piece_ids,
                    skip_special_tokens=True,
                    clean_up_tokenization_spaces=False,
                ).strip()
                if piece:
                    chunks.append(
                        ChunkData(
                            index=ix,
                            start=i,
                            end=i + 1,
                            text=piece,
                            kind="text",
                            token_count=len(piece_ids),
                        )
                    )
                    ix += 1
            i += 1
            continue

        piece = " ".join(tokens[i:best_end])
        chunks.append(
            ChunkData(
                index=ix,
                start=i,
                end=best_end,
                text=piece,
                kind="text",
                token_count=best_token_len,
            )
        )
        ix += 1
        if best_end >= n:
            break
        i = max(i + 1, best_end - overlap)
    return chunks


def is_table_separator(line: str) -> bool:
    text = (line or "").strip()
    if "|" not in text:
        return False
    cells = [cell.strip() for cell in text.strip("|").split("|")]
    if not cells:
        return False
    valid = [cell for cell in cells if re.fullmatch(r":?-{3,}:?", cell)]
    return len(valid) >= max(1, len(cells) - 1)


def split_table_rows(table_text: str, max_tokens: int) -> list[str]:
    lines = [line for line in table_text.splitlines() if line.strip()]
    if len(lines) <= 2:
        return [table_text]
    head = lines[:2]
    rows = lines[2:]
    parts: list[str] = []
    bucket: list[str] = []
    tokenizer = get_embed_tokenizer()
    head_tokens = count_token_ids(tokenizer, "\n".join(head))

    bucket_tokens = 0
    for row in rows:
        row_tokens = count_token_ids(tokenizer, row)
        if bucket and head_tokens + bucket_tokens + row_tokens > max_tokens:
            parts.append("\n".join(head + bucket))
            bucket = [row]
            bucket_tokens = row_tokens
            continue
        bucket.append(row)
        bucket_tokens += row_tokens

    if bucket:
        parts.append("\n".join(head + bucket))
    return parts or [table_text]


def collect_entity_terms(
    block: str,
    *,
    header_text: str | None = None,
    section_path: str | None = None,
) -> list[str]:
    block = block[:EMBEDDING_ENTITY_SCAN_MAX_CHARS]
    raw_terms: list[str] = []
    if header_text:
        raw_terms.extend(re.findall(r"[A-Za-zА-Яа-я0-9_.-]{3,}", header_text))
    if section_path:
        raw_terms.extend(re.findall(r"[A-Za-zА-Яа-я0-9_.-]{3,}", section_path))
    raw_terms.extend(re.findall(r"\b[A-ZА-Я0-9][A-Za-zА-Яа-я0-9_.-]{2,}\b", block))
    raw_terms.extend(
        [
            term
            for term in re.findall(r"\b[A-Za-zА-Яа-я0-9_.-]{3,}\b", block)
            if any(char.isdigit() for char in term)
            or "." in term
            or "_" in term
            or "-" in term
        ]
    )
    entity_terms: list[str] = []
    seen_terms = set()
    for term in raw_terms:
        normalized = term.strip(".,:;!?()[]{}").lower()
        if len(normalized) < 3 or normalized in seen_terms:
            continue
        seen_terms.add(normalized)
        entity_terms.append(normalized)
        if len(entity_terms) >= 12:
            break
    return entity_terms


def split_text_block_for_chunking(
    text: str,
    *,
    max_chars: int | None = None,
) -> list[str]:
    if max_chars is None:
        max_chars = EMBEDDING_BLOCK_MAX_CHARS

    normalized = (text or "").strip()
    if not normalized:
        return []
    if len(normalized) <= max_chars:
        return [normalized]

    segments: list[str] = []
    paragraphs = [
        part.strip() for part in re.split(r"\n\s*\n+", normalized) if part.strip()
    ]
    if len(paragraphs) > 1:
        for paragraph in paragraphs:
            segments.extend(
                split_text_block_for_chunking(paragraph, max_chars=max_chars)
            )
        return segments

    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?。！？])\s+", normalized)
        if part.strip()
    ]
    if len(sentences) > 1:
        bucket: list[str] = []
        bucket_len = 0
        for sentence in sentences:
            addition = len(sentence) if not bucket else len(sentence) + 1
            if bucket and bucket_len + addition > max_chars:
                segments.append(" ".join(bucket))
                bucket = [sentence]
                bucket_len = len(sentence)
                continue
            bucket.append(sentence)
            bucket_len += addition
        if bucket:
            segments.append(" ".join(bucket))
        return segments

    start = 0
    length = len(normalized)
    while start < length:
        end = min(length, start + max_chars)
        if end < length:
            split_at = normalized.rfind(" ", start, end)
            if split_at > start:
                end = split_at
        piece = normalized[start:end].strip()
        if piece:
            segments.append(piece)
        start = end
        while start < length and normalized[start].isspace():
            start += 1
    return segments


def chunk_document_text(
    text: str,
    *,
    max_tokens: int | None = None,
    overlap: int | None = None,
    boilerplate_hashes: frozenset[int] | None = None,
) -> list[ChunkData]:
    if max_tokens is None:
        max_tokens = EMBEDDING_CHUNK_MAX_TOKENS
    if overlap is None:
        overlap = EMBEDDING_CHUNK_OVERLAP_TOKENS

    chunks: list[ChunkData] = []
    lines = text.splitlines()
    blocks: list[tuple[str, str, str | None, str | None]] = []
    section_stack: list[str] = []
    text_lines: list[str] = []
    section_path: str | None = None
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            block = "\n".join(text_lines).strip()
            if block:
                blocks.append(
                    (
                        "text",
                        block,
                        section_stack[-1] if section_stack else None,
                        section_path,
                    )
                )
                text_lines = []
            level = len(heading.group(1))
            title = heading.group(2).strip()
            section_stack = section_stack[: level - 1]
            section_stack.append(title)
            section_path = " / ".join(section_stack)
            index += 1
            continue

        if (
            index + 1 < len(lines)
            and "|" in line
            and is_table_separator(lines[index + 1])
        ):
            block = "\n".join(text_lines).strip()
            if block:
                blocks.append(
                    (
                        "text",
                        block,
                        section_stack[-1] if section_stack else None,
                        section_path,
                    )
                )
                text_lines = []
            start = index
            index += 2
            while index < len(lines) and "|" in lines[index]:
                index += 1
            table_text = "\n".join(lines[start:index]).strip()
            if table_text:
                blocks.append(
                    (
                        "table",
                        table_text,
                        section_stack[-1] if section_stack else None,
                        section_path,
                    )
                )
            continue

        text_lines.append(line)
        index += 1

    block = "\n".join(text_lines).strip()
    if block:
        blocks.append(
            (
                "text",
                block,
                section_stack[-1] if section_stack else None,
                section_path,
            )
        )

    if boilerplate_hashes:
        blocks = [
            (kind, block, header_text, section_path)
            for kind, block, header_text, section_path in blocks
            if not is_boilerplate_block(block, boilerplate_hashes)
        ]

    chunk_ix = 0
    tokenizer = get_embed_tokenizer()
    for kind, block, header_text, section_path in blocks:
        entity_terms = collect_entity_terms(
            block,
            header_text=header_text,
            section_path=section_path,
        )

        if kind == "table":
            table_lines = [line for line in block.splitlines() if line.strip()]
            column_names = (
                [
                    cell.strip()
                    for cell in table_lines[0].strip("|").split("|")
                    if cell.strip()
                ]
                if table_lines
                else []
            )
            table_header = " | ".join(column_names) or header_text
            projection_lines = []
            if section_path:
                projection_lines.append(f"Section: {section_path}")
            if table_header:
                projection_lines.append(f"Table header: {table_header}")
            if column_names:
                projection_lines.append(
                    "Columns: " + ", ".join(column_names[: min(len(column_names), 12)])
                )
            if len(table_lines) > 2:
                projection_lines.append("Preview rows:")
                for row in table_lines[2 : min(len(table_lines), 5)]:
                    candidate = "\n".join([*projection_lines, row])
                    if count_token_ids(tokenizer, candidate) > max_tokens:
                        break
                    projection_lines.append(row)
            projection_text = "\n".join(projection_lines).strip()
            projection_terms = entity_terms[:]
            for column_name in column_names:
                normalized = column_name.strip().lower()
                if normalized and normalized not in projection_terms:
                    projection_terms.append(normalized)
            chunks.append(
                ChunkData(
                    index=chunk_ix,
                    start=None,
                    end=None,
                    text=projection_text,
                    kind="table",
                    header_text=table_header,
                    section_path=section_path,
                    entity_terms=projection_terms,
                    token_count=count_token_ids(tokenizer, projection_text),
                )
            )
            chunk_ix += 1
            for part in split_table_rows(block, max_tokens=max_tokens):
                chunks.append(
                    ChunkData(
                        index=chunk_ix,
                        start=None,
                        end=None,
                        text=part,
                        kind="table_rows",
                        header_text=table_header,
                        section_path=section_path,
                        entity_terms=projection_terms,
                        token_count=count_token_ids(tokenizer, part),
                    )
                )
                chunk_ix += 1
            continue

        block_words = block.split()
        if len(block_words) > 24:
            summary_text = " ".join(block_words[: min(len(block_words), 120)]).strip()
            summary_text = (
                f"Section: {section_path}\nSummary: {summary_text}"
                if section_path
                else f"Summary: {summary_text}"
            )
            chunks.append(
                ChunkData(
                    index=chunk_ix,
                    start=None,
                    end=None,
                    text=summary_text,
                    kind="section_summary" if section_path else "summary",
                    header_text=header_text,
                    section_path=section_path,
                    entity_terms=entity_terms,
                    token_count=count_token_ids(tokenizer, summary_text),
                )
            )
            chunk_ix += 1

        for segment in split_text_block_for_chunking(block):
            defs = chunk_text_word_window(
                segment,
                max_tokens=max_tokens,
                overlap=overlap,
            )
            for item in defs:
                chunks.append(
                    ChunkData(
                        index=chunk_ix,
                        start=item.start,
                        end=item.end,
                        text=item.text,
                        kind="text",
                        header_text=header_text,
                        section_path=section_path,
                        entity_terms=entity_terms,
                        token_count=item.token_count,
                    )
                )
                chunk_ix += 1

        if entity_terms:
            projection_lines = []
            if section_path:
                projection_lines.append(f"Section: {section_path}")
            projection_lines.append("Entities: " + ", ".join(entity_terms))
            projection_text = "\n".join(projection_lines)
            chunks.append(
                ChunkData(
                    index=chunk_ix,
                    start=None,
                    end=None,
                    text=projection_text,
                    kind="entity_projection",
                    header_text=header_text,
                    section_path=section_path,
                    entity_terms=entity_terms,
                    token_count=count_token_ids(tokenizer, projection_text),
                )
            )
            chunk_ix += 1

    return chunks


def validate_chunk_data(chunks: list[ChunkData], *, page_id: int) -> None:
    for chunk in chunks:
        if chunk.token_count > EMBEDDING_MAX_SEQ_LENGTH:
            raise EmbedderDocumentError(
                f"Chunk {chunk.index} for page {page_id} is too large for embedder "
                f"({chunk.token_count} tokens > {EMBEDDING_MAX_SEQ_LENGTH})",
                page_id=page_id,
            )
        if (
            chunk.kind in {"text", "table_rows"}
            and len(chunk.text) > EMBEDDING_BLOCK_MAX_CHARS
        ):
            raise EmbedderDocumentError(
                f"Chunk {chunk.index} for page {page_id} exceeds the block char cap "
                f"({len(chunk.text)} chars > {EMBEDDING_BLOCK_MAX_CHARS})",
                page_id=page_id,
            )
