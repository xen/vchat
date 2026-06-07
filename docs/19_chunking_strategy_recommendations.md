# Chunking Strategy Recommendations

Date: 2026-06-07

## Product Goal

The product goal is not to split documents into equal pieces. The goal is to give the user useful, grounded answers over indexed sites.

Good chunking must support these user outcomes:

- Find the exact page section that answers a question.
- Preserve enough local context for the model to answer without guessing.
- Provide citation-ready snippets with title, URL, section path, and readable text.
- Retrieve exact facts, names, numbers, table rows, policies, instructions, contacts, and product details.
- Avoid letting one noisy or pathological page dominate indexing time, storage, or retrieval results.
- Keep indexed content fresh enough that source reindexing remains operationally affordable.

This means chunking should be treated as product-facing information architecture, not only as an embedder preprocessing step.

## Current State

The current implementation already has a structured chunk contract:

- `Chunk.kind`
- `Chunk.header_text`
- `Chunk.section_path`
- `Chunk.entity_terms`
- `Chunk.token_count`
- `Chunk.text`
- `Chunk.embedding`
- `Chunk.fts`

Retrieval uses these fields:

- Vector search over `Chunk.embedding`.
- Full-text search over weighted `title`, `header_text`, `section_path`, `entity_terms`, and `text`.
- Reranking boosts `header_text`, `section_path`, `entity_terms`, table chunks, and summary chunks.
- Context assembly passes `kind`, `title`, `uri`, `header_text`, and `section_path` to the model.

So the correct direction is not "smaller chunks everywhere". The correct direction is better typed chunks with stronger structure and bounded costs.

## Main Problems

1. Segmentation is still too text-window oriented.

   The pipeline recognizes headings and markdown tables, then falls back to word windows. This loses important source semantics: navigation sections, FAQ entries, cards, definition lists, product blocks, contacts, tables, lists, and repeated page chrome.

2. Chunk size is controlled late.

   Large or malformed documents can reach the chunker as huge blocks. The current guards prevent some failures, but they do not make the ingestion shape predictable.

3. Runtime cost is driven by bad batches.

   Embedding throughput is now limited by batches with high total characters. Eight chunks can still mean tens of thousands of characters, so two workers do not double throughput.

4. Retrieval mixes different user intents.

   A user may ask for an exact quote, a table value, a summary, a list of options, or a procedural instruction. These need different chunk shapes and ranking signals.

5. Some generated chunks are useful for recall but weak for final answer context.

   `summary` and `entity_projection` chunks can improve candidate discovery, but if they are placed into final context too aggressively they can crowd out citation-ready source text.

## Recommended Chunk Taxonomy

Use typed chunks intentionally. Each type should have a retrieval purpose.

### `section_summary`

Purpose: high-recall routing into the right page section.

Content:

- Page title.
- Section path.
- Short extractive summary of the section.
- Key terms/entities.

Policy:

- One per meaningful section.
- Bounded to about 80-160 tokens.
- Good for vector and FTS recall.
- Lower priority for final answer context unless no better text chunk exists.

### `text`

Purpose: citation-ready answer content.

Content:

- Natural paragraph/list/instruction text.
- Include section heading prefix only when needed for disambiguation.

Policy:

- Target 180-350 embedding tokens.
- Hard cap 450-512 embedding tokens.
- Overlap 40-80 tokens only across same section and same block family.
- Prefer paragraph/list boundaries over fixed windows.

### `qa`

Purpose: FAQ and help-center retrieval.

Content:

- Question heading or FAQ prompt.
- Direct answer body.

Policy:

- Create when structure looks like FAQ, accordion, `h2/h3` question, or "Q:/A:".
- Target one answer per chunk.
- Strongly boost for question-like user prompts.

### `instruction`

Purpose: procedural answers.

Content:

- Procedure heading.
- Ordered steps.
- Required notes/warnings.

Policy:

- Keep steps together.
- Do not split a short procedure across chunks.
- For long procedures, split by step ranges and repeat procedure heading.

### `list`

Purpose: enumerations, feature lists, requirements, limitations.

Content:

- List heading.
- List items.

Policy:

- Keep short lists intact.
- Split long lists into stable ranges.
- Preserve bullets or numbering.

### `definition`

Purpose: exact concept, term, abbreviation, field, tariff, or parameter lookup.

Content:

- Term/name.
- Definition/value.
- Nearby qualifiers.

Policy:

- Extract from definition lists, tables, glossary-like pages, and "Term: value" patterns.
- Keep compact, usually under 120 tokens.

### `table`

Purpose: table discovery.

Content:

- Section path.
- Table caption/header.
- Column names.
- Small preview.

Policy:

- One table projection chunk per table.
- Strong for table-mode retrieval.
- Not enough for final numeric answer unless supported by `table_rows`.

### `table_rows`

Purpose: exact table values.

Content:

- Table caption/header.
- Column names.
- A bounded set of rows.

Policy:

- Repeat column names in every row chunk.
- Split by row groups, not token windows.
- Target 3-20 rows depending on width and token count.
- Preserve row text exactly enough for citation.

### `entity_projection`

Purpose: recall for names, model numbers, contacts, codes, dates, and URLs.

Content:

- Section path.
- Extracted entities.

Policy:

- Use for retrieval only.
- Do not include in final answer context unless explicitly needed.
- Keep very small.

### `page_summary`

Purpose: page-level routing and "what is this page about?"

Content:

- Page title.
- URL path signal.
- Main headings.
- Short extractive overview.

Policy:

- One per page, especially for long pages.
- Useful for broad questions and source discovery.

## Sizing Policy

Use different limits for different phases.

Extraction limits:

- `extraction_document_max_chars`: max canonical text saved for normal pages.
- `extraction_element_max_chars`: max text from one DOM node before splitting or skipping.
- `extraction_structure_max_blocks`: max structural blocks stored in metadata.
- `extraction_long_line_max_chars`: split or reject huge one-line payloads.

Segmentation limits:

- `chunk_section_max_chars`: max section text before subsections/fallback splitting.
- `chunk_block_max_chars`: max paragraph/list/table/code block.
- `chunk_max_tokens`: hard cap per embeddable chunk.
- `chunk_target_tokens`: target range, separate from hard cap.
- `chunk_overlap_tokens`: small and section-local.

Embedding batch limits:

- Limit by chunk count and total characters/tokens.
- Example: `max_chunks=8`, `max_chars=12000`, `max_tokens=3000`.
- Never allow one task to pick 8 very large chunks just because the count cap allows it.

Recommended initial values:

- Text target: 220-320 embedding tokens.
- Text hard cap: 450-512 embedding tokens.
- Text overlap: 50 tokens.
- Summary chunks: 80-160 tokens.
- Entity projection: 20-80 tokens.
- Table row chunk hard cap: 450-512 tokens.
- Embedding task total char cap: 12000-16000 chars.

## Segmentation Pipeline

Use a staged pipeline:

1. Extract canonical content from HTML/PDF/manual text.
2. Build compact structure with offsets, not duplicated full block text in `Page.meta`.
3. Classify blocks by type.
4. Remove boilerplate and low-value blocks before chunking.
5. Create typed chunks from structure.
6. Validate chunk bounds before writing rows.
7. Embed pending chunks using char/token-capped batches.

The chunker should operate on structural blocks, not only on a raw full string.

Block classification should detect:

- headings
- paragraphs
- lists
- FAQ entries
- procedural steps
- tables
- cards/product blocks
- definition/value blocks
- code/preformatted blocks
- contact/address blocks
- navigation/footer/sidebar boilerplate

Fallback splitting should be:

1. section boundary
2. paragraph boundary
3. list item boundary
4. sentence boundary
5. token window

Token window splitting should be the last resort.

## Retrieval Policy

Ranking should treat chunk types differently.

For broad "what is..." questions:

- Prefer `page_summary`, `section_summary`, then `text`.

For exact fact questions:

- Prefer `definition`, `text`, `table_rows`.

For procedural questions:

- Prefer `instruction`, then `list`, then `text`.

For table/numeric/comparison questions:

- Prefer `table`, then `table_rows`.

For quote/source requests:

- Prefer `text`, `qa`, `instruction`, `table_rows`.
- Avoid `summary` and `entity_projection` unless no direct source chunk exists.

For enumeration questions:

- Prefer multiple chunks from different section paths.
- Avoid over-selecting many chunks from the same page section.

This does not require replacing the current hybrid retrieval immediately. It requires making `kind` more expressive and adjusting rerank/context assembly policies.

## Quality Metrics

Add operational metrics:

- chunks per page p50/p90/p99/max
- tokens per chunk p50/p90/p99/max
- chars per embedding task p50/p90/p99/max
- embed seconds per chunk and per 1k chars
- pages marked oversized/truncated/skipped
- percentage of chunks by `kind`
- percentage of final answers using citation-ready chunks

Add retrieval quality evals:

- exact fact lookup
- FAQ answer lookup
- table value lookup
- procedural answer lookup
- quote/citation request
- broad summary request
- multi-section enumeration

Success should be measured by answer usefulness and citation accuracy, not only embedding throughput.

## Implementation Plan

### Phase 1: Stabilize Runtime

- Change pending embedding selection to cap total chars/tokens per task.
- Keep two embedder workers only if memory remains stable.
- Add metrics for batch total chars and duration.
- Add alerts for no embedding progress and p99 batch duration.

### Phase 2: Make Chunking Product-Aware

- Introduce richer `Chunk.kind` values: `qa`, `instruction`, `list`, `definition`, `page_summary`.
- Build chunks from structured blocks instead of raw text windows.
- Keep summaries and projections for recall, but control whether they enter final answer context.
- Add section-local overlap only for `text`.

### Phase 3: Fix Extraction Shape

- Stop storing full duplicated block content in `Page.meta["structure"]`.
- Store offsets, type, section path, and short previews.
- Add extraction gates for giant nodes, giant lines, giant tables, and low-value pages.
- Apply the same normalization path to manual uploads/edits.

### Phase 4: Tune Retrieval

- Adjust reranker bonuses by `kind` and query profile.
- Add source/section diversity rules for enumeration questions.
- Prefer citation-ready chunks for final context.
- Add eval cases and compare old/new chunking on the same source set.

## Recommended Product Decision

Adopt this principle:

> Chunk for answerability first, embedding convenience second.

That means:

- A chunk should usually answer one user intent, not merely fill a token window.
- Section and page summaries should route retrieval, but final answers should use direct source chunks.
- Tables, FAQs, lists, and procedures deserve first-class chunk types.
- Pathological pages should be bounded and visible, not allowed to silently consume indexing capacity.

## Open Questions

- Should oversized pages be partially indexed with a visible `truncated` marker, or skipped with `too_big`?
- Should `summary` chunks be generated extractively only, or should we later add LLM-generated summaries?
- Do we need per-source chunking profiles for docs, ecommerce, education sites, and news/blog sites?
- Should final answer context exclude `entity_projection` by default?
- What is the acceptable freshness SLA for a full-source reindex?
