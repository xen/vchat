# RAG Chunking Quality Loop

Date: 2026-06-07

Scope: local-only Ralph loop for chunking-first RAG quality improvement. No test
server actions, no deploy, no service restarts, no server reindex.

## Baseline

Local database: `postgresql://xen@localhost:5432/vchat`.

Corpus shape:

| Metric | Value |
| --- | ---: |
| Sources | 38 |
| Pages | 11,239 |
| Ready pages | 11,239 |
| Pages with content | 3,789 |
| Chunks | 39,201 |
| Chunks with embeddings | 0 |
| Chunks without embeddings | 39,201 |

Current local embeddings are empty, so the first local quality loop is based on
chunk materialization metrics and deterministic retrieval/context tests. Small
partial re-embedding remains allowed for later loops.

Chunk kind distribution:

| Kind | Chunks | Avg chars | P50 chars | P90 chars | P99 chars | Max chars | P99 tokens | Max tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| text | 18,594 | 4,107.3 | 768 | 11,993 | 12,000 | 12,000 | 3,500 | 3,500 |
| entity_projection | 12,548 | 167.5 | 173 | 246 | 329 | 516 | 90 | 183 |
| section_summary | 7,926 | 741.3 | 818 | 1,026 | 1,116 | 1,490 | 305 | 503 |
| summary | 53 | 994.5 | 888 | 1,644 | 2,522 | 2,522 | 1,076 | 1,076 |
| table | 40 | 394.4 | 310 | 642 | 1,070 | 1,070 | 283 | 283 |
| table_rows | 40 | 712.4 | 327 | 1,627 | 3,770 | 3,770 | 1,423 | 1,423 |

Chunk count per page:

| Metric | Value |
| --- | ---: |
| P50 chunks/page | 0 |
| P90 chunks/page | 12 |
| P99 chunks/page | 15 |
| Max chunks/page | 927 |
| P99 content chars/page | 98,154 |
| Max content chars/page | 1,033,208 |

Worst local pages by chunk count:

| Page | Source | Content chars | Chunks | Chunk chars | URL |
| ---: | ---: | ---: | ---: | ---: | --- |
| 46902 | 33 | 97,691 | 927 | 6,170,634 | `https://ksp.vbudushee.ru/public/home/documents` |
| 46906 | 33 | 97,697 | 927 | 6,170,640 | `https://ksp.vbudushee.ru/public/home/documents?tagId=949` |
| 46900 | 33 | 85,791 | 904 | 6,244,212 | `https://ksp.vbudushee.ru/identity/account/login` |
| 50073 | 41 | 77,320 | 613 | 171,501 | `https://www.pylot.me/assets/vendor/codemirror/CHANGELOG/` |
| 50047 | 41 | 47,069 | 322 | 88,900 | `https://www.pylot.me/articles/2022/10/17/osnovnie-strokovie-metodi-ispolzuemie-v-python/` |

Reproduced with current `chunk_document_text()`:

| Page | Content chars | Chunks | Chunk chars | Kind counts |
| ---: | ---: | ---: | ---: | --- |
| 46902 | 97,691 | 927 | 6,170,634 | `summary=1`, `text=925`, `entity_projection=1` |
| 50073 | 77,320 | 613 | 171,501 | mixed text/summary/projection |
| 50047 | 47,069 | 322 | 88,900 | mixed text/summary/projection |
| 46566 | 89,079 | 92 | 79,397 | mostly text |

Important observation: page `46902` content starts with raw `<!DOCTYPE html>`.
The chunker currently treats that payload as plain text. Combined with
`embedding_chunk_overlap_tokens=400`, the window can advance by only a small
number of words when markup-heavy words tokenize into many subword tokens. This
creates massive duplicate chunk text and makes embeddings/retrieval unusable.

Existing focused retrieval tests:

```text
93 passed in 5.70s
```

## Initial Hypotheses

| Idea | Expected user-quality impact | Expected runtime impact | Risk | Recommendation |
| --- | --- | --- | --- | --- |
| Make `chunk_text_word_window()` token-aware and cap overlap by actual chunk token count | High: fewer duplicate chunks, less repeated boilerplate in retrieval context | High: cuts chunk count and embedding queue for pathological pages | Medium: changes chunk boundaries | Do now |
| Add HTML/plain-text normalization gate before chunking raw `Page.content` | High for pages saved as raw HTML | Medium-high | Medium: needs careful source metadata policy | Next loop |
| Add metadata-only policy for giant CSV/statistical dumps | High for noisy file pages | High for storage/queue | Medium: product-visible indexing policy | Next loop after chunker window fix |
| Exclude `entity_projection` from final context by default | Medium, but downstream of chunk shape | Low | Low | Later |
| Demote summaries in final context | Medium | Low | Low-medium | Later |

## First Intervention Decision

Implement a small chunker redesign in the sliding-window primitive:

- tokenize words once;
- build chunks by actual embedding token counts and char caps;
- treat configured overlap as tokens, not word count;
- cap effective overlap to a bounded fraction of the actual chunk;
- guarantee forward progress even when configured overlap exceeds a small chunk.

This directly targets the measured failure without changing schema, ingestion
state, server runtime, or existing source records.

## Intervention 1 Result

Changed `chunk_text_word_window()` to build windows from actual embedding token
counts instead of using the configured token overlap as a word overlap.

Key behavior:

- each word is tokenized once;
- chunk growth stops on token cap or char cap;
- long single tokens are still split by token IDs;
- effective overlap is bounded to at most 25% of the current chunk token count;
- progress is guaranteed even when the configured overlap is larger than the
  current chunk.

Local before/after on measured pages:

| Page | Before chunks | After chunks | Before chunk chars | After chunk chars | Assessment |
| ---: | ---: | ---: | ---: | ---: | --- |
| 46902 | 927 | 84 | 6,170,634 | 101,139 | fixed pathological duplication |
| 46906 | 927 | 84 | 6,170,640 | 101,145 | fixed pathological duplication |
| 46900 | 904 | 49 | 6,244,212 | 91,546 | fixed pathological duplication |
| 50073 | 613 | 613 | 171,501 | 171,501 | unchanged; issue is many small blocks/projections |
| 50047 | 322 | 322 | 88,900 | 88,900 | unchanged; issue is many small blocks/projections |
| 46566 | 92 | 92 | 79,397 | 74,999 | slightly less overlap |

The biggest measured page now has chunk text about the same order as source
content instead of about 63x larger. This should directly reduce embedding
queue size, storage, and duplicate retrieval noise for markup-heavy pages.

Tests:

```text
venv/bin/pytest tests/test_embedder_chunking_limits.py tests/test_document_shingles.py -q
33 passed in 0.51s

venv/bin/pytest tests/test_retrieval_ctx.py tests/test_chat_retrieval_ctx.py tests/chat/test_ctx_module.py tests/test_coverage_boost_misc.py -q
93 passed in 5.28s

venv/bin/ruff check jobs/embedder/chunking.py tests/test_embedder_chunking_limits.py
All checks passed!

venv/bin/pytest -q
471 passed, 2 skipped, 2 warnings in 6.09s
```

The first full `venv/bin/pytest -q` attempt failed inside the sandbox because
tests touched localhost Redis and external DNS. Re-running the same command with
approved local network permissions passed.

Decision: keep. The intervention addresses the primary measured chunking bug
without schema changes or server-side actions.

## Next Loop

The remaining high-priority chunking failures are not fixed by token-aware
overlap alone:

1. Some `Page.content` rows contain raw HTML. The chunker should not index tags
   and scripts as natural-language answer content.
2. Pages like the CodeMirror changelog still create hundreds of chunks through
   many small text/summary/entity-projection blocks. The next loop should add a
   typed policy for code/changelog/vendor assets and reduce projection/summary
   explosion.
3. Giant CSV/statistical files need metadata-only indexing so the assistant can
   cite the file without embedding the whole dump.

Recommended next intervention: add an explicit content-classification gate before
chunk materialization for raw HTML/code/vendor/statistical documents, then run
the same bounded local before/after eval on pages `46902`, `50073`, `50047`, and
the known giant CSV page.

## Loop 2: Raw HTML Content Normalization

### Measure

Local corpus contains old/stale rows where `Page.content` is still a full HTML
document rather than extracted markdown-like content:

| Metric | Value |
| --- | ---: |
| Raw HTML pages | 145 |
| Raw HTML content chars | 19,820,534 |

CSV-like pages in the current local slice were not present:

| Metric | Value |
| --- | ---: |
| CSV/TSV pages | 0 |
| CSV/TSV content chars | 0 |

Vendor/code-like asset pages are present but are a separate policy class:

| Metric | Value |
| --- | ---: |
| Vendor/code-like pages | 18 |
| Vendor/code-like content chars | 139,467 |

Worst vendor/code-like page:

| Page | URL | Content chars | Current chunks |
| ---: | --- | ---: | ---: |
| 50073 | `https://www.pylot.me/assets/vendor/codemirror/CHANGELOG/` | 77,320 | 613 |

### Hypothesis

Full HTML documents should be normalized to visible text before chunk
segmentation. Indexing `<html>`, `<head>`, `<script>`, `<style>`, navigation, and
HTML attributes as answer evidence creates retrieval noise and unnecessary
embedding cost.

This is a chunking-stage repair for stale local rows and other non-crawler write
paths that may still pass raw full HTML into `Page.content`. It does not replace
the crawler extraction pipeline.

### Intervention

Added `normalize_html_document_for_chunking()` before `chunk_document_text()`
structural splitting:

- only triggers for full HTML documents starting with `<!doctype html>` or
  `<html...>`;
- removes script/style/noscript/template/svg/header/footer/nav/aside/dialog;
- removes password forms;
- extracts body visible text line-by-line;
- leaves inline mentions like `Use the <html> tag` untouched.

### Eval

Local before/after on representative pages:

| Page | State | Chunks | Chunk chars |
| ---: | --- | ---: | ---: |
| 46902 | baseline | 927 | 6,170,634 |
| 46902 | after token-overlap fix | 84 | 101,139 |
| 46902 | after HTML normalization | 3 | 1,113 |
| 46906 | after HTML normalization | 3 | 1,113 |
| 46900 | after HTML normalization | 2 | 90 |
| 47061 | after HTML normalization | 3 | 3,903 |
| 46508 | after HTML normalization | 3 | 892 |
| 50073 | after HTML normalization | 613 | 171,501 |
| 50047 | after HTML normalization | 322 | 88,900 |
| 46566 | after HTML normalization | 3 | 6,950 |

The unchanged `50073`/`50047` cases are expected: they are not full HTML document
payloads. They remain candidates for the next content-policy loop.

Tests:

```text
venv/bin/pytest tests/test_embedder_chunking_limits.py tests/test_document_shingles.py -q
35 passed in 0.69s

venv/bin/pytest tests/test_retrieval_ctx.py tests/test_chat_retrieval_ctx.py tests/chat/test_ctx_module.py tests/test_coverage_boost_misc.py -q
93 passed in 4.25s

venv/bin/ruff check jobs/embedder/chunking.py tests/test_embedder_chunking_limits.py
All checks passed!

venv/bin/pytest -q
473 passed, 2 skipped, 2 warnings in 7.41s
```

One focused retrieval command was accidentally run in parallel with the full
suite and aborted during native `torch`/`spacy` imports. Re-running the focused
retrieval tests alone passed, so the abort is not treated as a code regression.

### Review

Decision: keep.

Reasoning:

- It improves a measured real corpus defect: full HTML rows no longer produce
  tag/script/navigation chunks.
- It is scoped to full HTML documents and does not change inline HTML mentions.
- It does not introduce schema changes, server actions, silent retries, or legacy
  compatibility paths.

Remaining risk:

- Some body-visible JSON/config fragments can still survive extraction, for
  example `{"isActive":false,"cookieKey":null}` on document-list pages. Filtering
  these should be a separate measured intervention because aggressive JSON-line
  removal could hide useful structured content.

Next recommended loop: content policy for vendor/code/changelog assets and
metadata-only file summaries. The first measured target is page `50073`.

## Loop 3: Metadata-Only Policy For Vendor And Giant Data Files

### Measure

After raw HTML normalization, vendor/code-like assets remain a separate chunking
problem. The current local corpus has:

| Metric | Value |
| --- | ---: |
| Vendor/code-like pages | 18 |
| Vendor/code-like content chars | 139,467 |

Largest measured offender:

| Page | URL | Content chars | Existing chunks |
| ---: | --- | ---: | ---: |
| 50073 | `https://www.pylot.me/assets/vendor/codemirror/CHANGELOG/` | 77,320 | 613 |

The current local corpus has no CSV/TSV pages, but the known Dota CSV case from
earlier work remains a required policy target for future local/server data.

### Hypothesis

Vendor assets, large code assets, and giant CSV/statistical dumps should remain
discoverable, but should not be indexed as full answer text. A single metadata
chunk is enough for the assistant to say the document/file exists and provide the
URL/title/type/preview.

### Intervention

Added metadata-only materialization policy in `jobs/crawler/tasks.py`:

- explicit `meta["index_policy"] == "metadata_only"` is respected;
- giant `.csv`/`.tsv` or CSV content-type files become metadata-only when large
  by extracted content, raw size, or embedder document cap;
- `/assets/vendor/` and `/node_modules/` paths become metadata-only;
- large `doc_type=code` assets become metadata-only;
- materialization creates exactly one `file_summary` chunk;
- page metadata records `index_policy=metadata_only` and
  `index_policy_reason`.

The policy is applied before full-text chunking and before marking an oversized
document as `too_big`, but only for explicit file/code/data policy matches.
Ordinary oversized pages still use the existing `too_big` fail-fast path.

### Eval

Real local page `50073`, evaluated through `materialize_page_chunks()` with a
fake session and no database write:

| Page | Before chunks | After chunks | After kind | Policy reason |
| ---: | ---: | ---: | --- | --- |
| 50073 | 613 | 1 | `file_summary` | `vendor_asset` |

Generated summary shape:

```text
Document indexed as metadata only.
Title: CHANGELOG
URL: https://www.pylot.me/assets/vendor/codemirror/CHANGELOG/
Document type: html
Content type: text/html; charset=utf-8
Index policy reason: vendor_asset
Content length: 77320 chars
Preview: # CHANGELOG ## 5.65.16 (2023-11-20) ### Bug fixes
```

Tests:

```text
venv/bin/pytest tests/test_embedder_chunking_limits.py tests/test_crawler_overhaul.py::TestSoft404Pages::test_pipeline_marks_oversize_content_too_big_without_scheduling -q
16 passed in 0.54s

venv/bin/ruff check jobs/crawler/tasks.py jobs/embedder/chunking.py tests/test_embedder_chunking_limits.py
All checks passed!

venv/bin/pytest -q
475 passed, 2 skipped, 2 warnings in 5.61s
```

A broader focused command that included `tests/test_embedder_parallelism.py`
failed on `Embedding device mps was requested but is unavailable`; this is an
environment-sensitive existing test condition and not caused by the metadata
policy. The full suite passed afterward.

### Review

Decision: keep.

Reasoning:

- The change removes a measured 613-chunk vendor asset from full-text indexing
  while preserving a citation-ready file summary.
- It adds the required `metadata_only` policy marker.
- It covers giant CSV/statistical dumps by URI/content-type/size without
  changing ordinary oversized page behavior.
- It avoids server-side actions, schema changes, broad fallbacks, or hidden
  retries.

Remaining risk:

- The policy is intentionally conservative for `doc_type=code`; small code-like
  pages are still fully indexed. More aggressive code/document policies should
  be measured against real user queries before changing them.

Next recommended loop: build a small executable RAG quality eval fixture around
the measured page classes (`normal page`, `raw HTML page`, `vendor metadata-only
page`, `absent answer`) so future chunking changes are compared on retrieval and
answer-grounding behavior, not only chunk counts.

## Loop 4: Executable Chunking Policy Eval Base

### Measure

The first three loops used local SQL measurements, ad hoc bounded page
rematerialization, and unit tests. That was enough to validate the immediate
fixes, but it did not yet create a reusable eval base as required by the broader
RAG quality prompt.

### Hypothesis

A small repo-native fixture plus an executable pytest runner will make the
measured chunking policies regression-checkable. It should cover the failure
classes already observed locally before expanding to answer-level evals.

### Intervention

Added:

- `tests/fixtures/rag_quality/chunking_policy_cases.json`
- `tests/rag_quality/test_chunking_policy_eval.py`

Current fixture cases:

| Case | Coverage |
| --- | --- |
| `normal_markdown_text_stays_full_text` | normal natural-language page remains full-text indexed |
| `full_html_document_indexes_visible_text_only` | full raw HTML strips script/style/nav and keeps visible answer text |
| `vendor_changelog_is_metadata_only` | vendor changelog becomes one `file_summary` chunk |
| `giant_csv_is_metadata_only` | giant CSV/statistical file becomes one `file_summary` chunk |

### Eval

```text
venv/bin/pytest tests/rag_quality/test_chunking_policy_eval.py -q
4 passed in 0.42s

venv/bin/pytest tests/test_embedder_chunking_limits.py tests/test_document_shingles.py tests/rag_quality/test_chunking_policy_eval.py tests/test_crawler_overhaul.py::TestSoft404Pages::test_pipeline_marks_oversize_content_too_big_without_scheduling -q
42 passed in 0.67s

venv/bin/pytest tests/test_retrieval_ctx.py tests/test_chat_retrieval_ctx.py tests/chat/test_ctx_module.py tests/test_coverage_boost_misc.py -q
93 passed in 4.26s

venv/bin/ruff check jobs/crawler/tasks.py jobs/embedder/chunking.py tests/test_embedder_chunking_limits.py tests/rag_quality/test_chunking_policy_eval.py
All checks passed!

venv/bin/pytest -q
479 passed, 2 skipped, 2 warnings in 5.75s
```

Native import aborts were observed when running heavy retrieval tests in
parallel with other pytest processes. The same retrieval tests pass when run
alone, and the full suite passes in one process.

### Review

Decision: keep.

This is still not the full answer-level benchmark requested by
`docs/20_rag_quality_improvement_prompt.md`, but it is a concrete regression
base for the chunking/content-policy defects fixed in loops 1-3.

Next recommended loop: extend the eval base upward from chunking policy into
retrieval/context behavior. Minimal next cases should cover exact fact lookup,
negative/absent answer, metadata-only document discovery, and raw HTML page
answerability.

## Loop 5: Retrieval Context Policy For Metadata-Only Documents

### Measure

Before this loop, a metadata-only `file_summary` chunk with a URL was included in
context, but `_build_policy_and_coverage()` did not count it as quote/source
ready evidence:

```text
ContextPolicy(
  quote_mode=True,
  has_source_url=True,
  has_quote_candidate=False,
  reason_code='missing_quote_candidate'
)
coverage = {
  'section_count': 0,
  'quote_ready': False
}
```

This contradicted the metadata-only product goal: for downloadable/data/vendor
documents, the assistant should be able to answer that the file exists and cite
the download/source URL.

### Hypothesis

`file_summary` should be treated as a citation-ready evidence kind for
source/download/document-discovery queries. It should also get a small rerank
bonus below `text` so metadata-only document summaries are not unfairly buried
behind generic text chunks when the query mentions a filename or file type.

### Intervention

Updated `vchat/views/chat/ctx.py`:

- added `file_summary` to `RERANK_KIND_BONUS` with a lower bonus than `text`;
- added `file_summary` to quote-ready context kinds;
- added `file_summary` to coverage refs so policy coverage can count it.

### Eval

After the change, the same deterministic measurement returns:

```text
ContextPolicy(
  quote_mode=True,
  has_source_url=True,
  has_quote_candidate=True,
  reason_code='ok'
)
coverage = {
  'section_count': 1,
  'quote_ready': True
}
```

Tests:

```text
venv/bin/pytest tests/test_retrieval_ctx.py tests/chat/test_ctx_module.py -q
58 passed in 4.70s

venv/bin/ruff check vchat/views/chat/ctx.py tests/test_retrieval_ctx.py tests/chat/test_ctx_module.py jobs/crawler/tasks.py jobs/embedder/chunking.py tests/test_embedder_chunking_limits.py tests/rag_quality/test_chunking_policy_eval.py
All checks passed!

venv/bin/pytest -q
482 passed, 2 skipped, 2 warnings in 5.66s
```

### Review

Decision: keep.

Reasoning:

- The change closes the retrieval/context gap introduced by metadata-only
  indexing: file summaries are now both discoverable and source-ready.
- It is narrow: no schema changes, no fallback behavior, no server actions.
- It preserves stronger preference for direct `text` chunks by keeping the
  `file_summary` bonus lower than `text`.

Remaining gap:

- This still does not prove final LLM answer groundedness. The next loop should
  add an answer/context eval runner that checks source precision, negative
  answers, and citation payloads over deterministic snippets or a small local
  fixture corpus.

## Loop 6: Deterministic Answer Grounding Eval Base

### Measure

Before this loop, the repo had an executable chunking-policy eval and focused
retrieval/context unit tests, but no answer-level RAG quality fixture matching
the Phase 3 requirements in `docs/20_rag_quality_improvement_prompt.md`.

Measured local gap:

| Eval layer | State before loop |
| --- | --- |
| Chunking/content policy fixture | present |
| Retrieval/context policy tests | present |
| Answer groundedness fixture | absent |
| Citation URL precision fixture | absent |
| Negative/absent answer fixture | absent |

### Hypothesis

A deterministic answer-level fixture can catch the highest-risk regressions
without calling a live model: missing required facts, hallucinated claims,
citations that point at the wrong URL, source kind misuse, and negative queries
that invent answers. This does not replace live answer generation evals, but it
creates a stable contract that future local/live runners can reuse.

### Intervention

Added:

- `tests/fixtures/rag_quality/answer_grounding_cases.json`
- `tests/rag_quality/test_answer_grounding_eval.py`

The fixture stores the fields required by the prompt:

- user query;
- expected source URLs and titles;
- expected answer facts;
- forbidden claims;
- forbidden citation URLs/source kinds;
- whether citation is required;
- acceptable answer notes;
- current baseline result.

Current deterministic case coverage:

| Case type | Fixture case |
| --- | --- |
| Exact fact lookup | `exact_fact_lookup_course_start` |
| FAQ/help answer | `faq_help_answer_password_reset` |
| Procedural/instruction answer | `procedural_instruction_answer_export_csv` |
| Table/numeric lookup | `table_numeric_lookup_price` |
| Quote/source request | `quote_source_request_policy_sentence` |
| Broad page/source summary | `broad_page_summary_release_notes` |
| Multi-section enumeration | `multi_section_enumeration_requirements` |
| Negative query where answer is absent | `negative_absent_answer_no_ios_app` |
| Noisy source context | `noisy_context_uses_relevant_source_only` |
| Downloadable document query | `downloadable_document_metadata_only` |

The runner validates:

- required case-type coverage;
- required facts are present;
- forbidden claims are absent;
- citation IDs exist in the source payload;
- required URLs are cited when citations are required;
- forbidden noisy URLs are not cited;
- forbidden source kinds are not used for cited evidence;
- absent-answer cases say the content was not found and do not cite irrelevant
  sources.

### Eval

```text
venv/bin/pytest tests/rag_quality/test_answer_grounding_eval.py -q
11 passed in 0.04s

venv/bin/ruff check tests/rag_quality/test_answer_grounding_eval.py
All checks passed!

venv/bin/pytest tests/rag_quality -q
15 passed in 0.41s
```

### Review

Decision: keep.

Reasoning:

- It implements the requested repo-native RAG quality eval base without server
  actions, Redis changes, or live reindexing.
- It covers every Phase 3 answer category in the prompt.
- It is intentionally deterministic, so it can run in normal CI and can later
  be extended with local retrieval/context snapshots and live answer captures.

Remaining gap:

- The fixture currently validates stored answer/source payloads, not actual
  streamed model output. The next loop should connect these cases to a bounded
  local pipeline run: chunk fixture content, retrieve/build context, generate or
  capture an answer, then run the same groundedness checks against the produced
  answer.

## Loop 7: Citation Payload Alignment In Final Context

### Measure

While preparing answer-grounding evals, the final JSON RAG context payload had a
contract gap:

- `ContextPayload.snippets[]` did not include an explicit `citation_id`;
- the system prompt requires answers to use `[[citation:ID]]`;
- source payloads and used chunks were built from the full post-rerank snippet
  list, not from the budget-truncated snippets actually sent to the model.

This means the assistant could only infer citation IDs from array order, and the
UI/debug payload could include sources that were not visible to the model after
context trimming.

### Hypothesis

Answer citation precision improves if the context payload, `sources`, and
`used_chunks` are all generated from the same selected snippet list and each
visible snippet carries an explicit citation ID.

### Intervention

Updated `vchat/views/chat/ctx.py`:

- added `citation_id` to `ContextSnippet`;
- extracted `select_context_snippets()` so token-budget selection happens once
  and can be reused;
- builds policy, context JSON, used chunks, and source payloads from the same
  selected snippets;
- emits one source payload per visible citation ID instead of deduplicating away
  citation IDs.

Added tests in `tests/chat/test_ctx_module.py`:

- context JSON includes explicit sequential citation IDs;
- when context token budget trims snippets, `sources` and `used_chunks` contain
  only the snippets visible in the context payload.

### Eval

The first focused command was accidentally run next to another import-heavy
process and hit the known native `torch`/`spacy` abort before test execution.
Separate focused runs passed:

```text
venv/bin/pytest tests/chat/test_ctx_module.py -q
15 passed in 4.20s

venv/bin/pytest tests/test_retrieval_ctx.py -q
45 passed in 3.97s

venv/bin/ruff check vchat/views/chat/ctx.py tests/chat/test_ctx_module.py
All checks passed!

venv/bin/pytest -q
495 passed, 2 skipped, 2 warnings in 5.33s
```

### Review

Decision: keep.

Reasoning:

- It directly addresses a Phase 5 prompt requirement: citation/source payloads
  must align with the chunks available to answer generation.
- The change is narrow and local to context assembly.
- It does not add fallback behavior, server actions, schema changes, or legacy
  compatibility paths.

Remaining gap:

- The answer-grounding fixture still needs a live or captured answer-generation
  runner. The new explicit `citation_id` contract makes that next loop easier:
  the runner can now compare produced `[[citation:N]]` markers directly against
  the context/source payload IDs.

## Loop 8: Fixture Context Pipeline For Answer Grounding

### Measure

After Loop 7, the answer-grounding fixture validated saved answer/source
payloads, and product code emitted explicit `citation_id` values in RAG context.
However, the eval still did not prove that the fixture sources could pass
through the local context builder into a model-visible context payload.

Measured gap:

| Check | State before loop |
| --- | --- |
| Answer facts/forbidden claims | covered |
| Saved answer citations vs saved source payload | covered |
| Fixture sources transformed through `build_context_from_snippets()` | absent |
| Answer citations vs model-visible context payload | absent |
| Noisy context present but not cited | only saved-payload check |

### Hypothesis

The next answer-quality regression layer should run fixture source snippets
through the actual local context builder. This catches broken `citation_id`
serialization, budget/context selection mistakes, and citation references that
do not exist in the model-visible context.

### Intervention

Extended `tests/fixtures/rag_quality/answer_grounding_cases.json`:

- every source now includes snippet text;
- the noisy-context case marks the expected irrelevant source URL that should be
  present in context but not cited.

Extended `tests/rag_quality/test_answer_grounding_eval.py`:

- builds `ctx.Snippet` objects from fixture sources;
- calls `ctx.build_context_from_snippets()` with a deterministic token counter;
- parses the emitted JSON context payload;
- verifies answer citation IDs exist in that context payload;
- verifies expected source URLs are present in model-visible context;
- verifies citation-required cases cite expected context URLs;
- verifies noisy URLs can be present in context without being cited.

### Eval

```text
venv/bin/pytest tests/rag_quality/test_answer_grounding_eval.py -q
21 passed in 3.97s

venv/bin/ruff check tests/rag_quality/test_answer_grounding_eval.py
All checks passed!

venv/bin/pytest tests/rag_quality -q
25 passed in 4.07s
```

### Review

Decision: keep.

Reasoning:

- It moves the eval base from static answer/source checking toward a local
  end-to-end slice: fixture source snippets -> context builder -> answer
  citation validation.
- It directly covers source precision, context noise, and model-visible citation
  availability from the prompt.
- It remains local-only and deterministic: no server actions, no Redis, no
  reindexing, no live model call.

Remaining gap:

- The runner still uses captured fixture answers instead of invoking a model.
  The next loop should either add captured local answer outputs from the current
  provider path or introduce a bounded optional live-answer eval command that
  reuses these deterministic checks.

## Loop 9: Negative Answer Policy In Generation Prompt

### Measure

`docs/20_rag_quality_improvement_prompt.md` explicitly calls out negative
queries and asks whether answer generation has enough policy to say "not found
in indexed sources".

Before this loop, `SYSTEM_PROMPT` contained:

- a grounding rule: never create or infer information not grounded in factual
  context;
- a citation rule: use `[[citation:ID]]` when referring to context.

It did not explicitly instruct the model what to do when indexed context does
not contain the answer. That leaves room for either guessing or citing unrelated
retrieved context.

### Hypothesis

Adding an explicit absent-answer instruction to the generation prompt should
improve behavior on negative queries, especially when retrieval returns nearby
but non-answering snippets.

### Intervention

Updated `vchat/views/chat/views.py` `SYSTEM_PROMPT`:

- if indexed context does not contain the requested answer, say it was not found
  in the indexed sources;
- do not guess;
- do not cite unrelated context.

Added `tests/chat/test_system_prompt_policy.py` to lock this generation policy.

### Eval

```text
venv/bin/pytest tests/chat/test_system_prompt_policy.py tests/rag_quality/test_answer_grounding_eval.py -q
22 passed in 3.98s

venv/bin/ruff check vchat/views/chat/views.py tests/chat/test_system_prompt_policy.py tests/rag_quality/test_answer_grounding_eval.py
All checks passed!
```

### Review

Decision: keep.

Reasoning:

- It addresses a measured prompt-policy gap tied directly to the negative-answer
  eval case.
- It is a narrow answer-generation instruction, not a fallback path or silent
  recovery.
- It avoids server actions and does not touch provider/runtime configuration.

Remaining gap:

- Prompt policy still needs live or captured model-output evaluation. The next
  loop should run the negative/noisy cases through a bounded answer generation
  path and validate the output with the existing deterministic checker.

## Loop 10: Captured Generation Envelope Eval

### Measure

After Loop 9, the eval suite could validate captured answers against saved
sources and against context payloads. It still did not check the complete
generation input envelope used by the provider path:

- system prompt;
- developer RAG context message;
- user query;
- captured answer citations against that exact context payload.

Calling a live provider is intentionally not mandatory for the normal local test
suite because it would require external network/API state. The measured gap is
therefore the absence of a deterministic captured-output runner that verifies
the prompt/context/query envelope.

### Hypothesis

A captured generation-envelope eval is the next stable step before optional live
model evals. It should prove that each fixture answer is evaluated against the
same system prompt and context payload shape that answer generation receives.

### Intervention

Extended `tests/rag_quality/test_answer_grounding_eval.py`:

- builds generation messages as `system`, `developer`, `user`;
- uses current `SYSTEM_PROMPT`;
- uses real `ctx.build_context_from_snippets()` output for the developer
  context message;
- verifies the system prompt contains citation and negative-answer policy;
- verifies the user message matches the fixture query;
- validates captured answer citations against the exact context payload in the
  generated developer message.

### Eval

The first focused run hit the known native import abort while importing the
guardrails/spacy/torch stack before test execution. Re-running the same test
alone passed:

```text
venv/bin/pytest tests/rag_quality/test_answer_grounding_eval.py -q
31 passed in 3.97s

venv/bin/ruff check tests/rag_quality/test_answer_grounding_eval.py
All checks passed!

venv/bin/pytest tests/rag_quality -q
35 passed in 4.03s
```

### Review

Decision: keep.

Reasoning:

- It connects captured answers to the actual generation message envelope without
  requiring a live provider.
- It further covers source precision and negative-answer policy from the prompt.
- It remains local-only and deterministic.

Remaining gap:

- The suite still does not execute `ai_chat_stream()` with a live model. The
  next loop can add an opt-in live-answer command or a fake streaming provider
  harness that exercises `ai_chat_stream()` event collection while reusing the
  same answer-grounding checks.

## Loop 11: Citation ID Policy In Generation Prompt

### Measure

Loop 7 added explicit `citation_id` values to the JSON RAG context snippets.
Loop 10 verifies captured answers against the generated message envelope.

The remaining prompt gap was that `SYSTEM_PROMPT` only said to use inline
citations in the format `[[citation:ID]]`; it did not explicitly tell the model
that IDs must come from the provided context snippets. That leaves room for
invented citation IDs even when the context payload is correct.

### Hypothesis

The generation prompt should state the citation ID contract directly: use only
IDs present in context snippets and never invent citation IDs.

### Intervention

Updated `vchat/views/chat/views.py` `SYSTEM_PROMPT`:

- use only citation IDs that appear in provided context snippets;
- never invent citation IDs.

Strengthened:

- `tests/chat/test_system_prompt_policy.py`
- `tests/rag_quality/test_answer_grounding_eval.py`

### Eval

```text
venv/bin/pytest tests/chat/test_system_prompt_policy.py tests/rag_quality/test_answer_grounding_eval.py -q
33 passed in 3.82s

venv/bin/ruff check vchat/views/chat/views.py tests/chat/test_system_prompt_policy.py tests/rag_quality/test_answer_grounding_eval.py
All checks passed!
```

### Review

Decision: keep.

Reasoning:

- It closes the prompt-side half of the citation alignment work from Loops 7 and
  10.
- It is narrow and policy-only; no fallback logic or server/runtime changes.
- It gives the future live-answer eval a stricter expected behavior to measure.

Remaining gap:

- Still no mandatory live model eval in the normal suite. The next loop should
  focus on a local fake streaming harness or an opt-in live command for
  `ai_chat_stream()` outputs.

## Loop 12: Fake Streamed Answer Eval Over `ai_chat_stream()`

### Measure

After Loop 11, the eval suite validated:

- captured answer text;
- fixture source snippets through the local context builder;
- captured generation message envelope.

It still did not execute the actual `ai_chat_stream()` event path. That left one
important local gap before optional live model calls: streamed provider chunks
could be accumulated incorrectly or diverge from the answer text passed to the
groundedness checker.

### Hypothesis

A fake streaming guardrails client can exercise `ai_chat_stream()` without
network access and then reuse the same groundedness/citation checks against the
assembled assistant message.

### Intervention

Extended `tests/rag_quality/test_answer_grounding_eval.py`:

- added a fake OpenAI-compatible provider/model context;
- monkeypatches `get_guardrails_client()` to return an async fake streaming
  client;
- splits each fixture answer into streamed content chunks;
- calls `chat_views.ai_chat_stream()` with the generated context/user messages;
- verifies the request messages equal the expected generation envelope;
- verifies streamed content accumulation equals the captured answer;
- verifies final `assistant_message.content` passes the shared groundedness and
  citation checks.

### Eval

```text
venv/bin/pytest tests/rag_quality/test_answer_grounding_eval.py -q
41 passed in 3.85s

venv/bin/ruff check tests/rag_quality/test_answer_grounding_eval.py
All checks passed!

venv/bin/pytest tests/rag_quality -q
45 passed in 3.94s
```

### Review

Decision: keep.

Reasoning:

- It moves the eval one step closer to real answer generation by exercising the
  stream collector and final assistant message path.
- It remains deterministic and local-only: no server actions, no Redis, no
  provider network call.
- It reuses the same answer-grounding checks rather than creating a separate
  weaker assertion path.

Remaining gap:

- This still uses captured answers rather than model-generated answers. The next
  optional loop should add an explicitly opt-in live-answer eval command, gated
  outside the normal test suite, so local development remains deterministic.

## Loop 13: Opt-In Live Answer Eval Command

### Measure

After Loop 12, normal tests exercised `ai_chat_stream()` with fake streamed
answers. The remaining gap was live model output: the suite still had no command
that could run selected fixture cases through the real provider path and reuse
the deterministic groundedness checks.

This must remain opt-in because live provider calls depend on local credentials,
network access, and model behavior. The normal test suite should stay
deterministic and local-only.

### Hypothesis

An explicit CLI runner can provide a bounded local live-answer eval without
making normal tests depend on external provider state. The runner should default
to one case, support selecting specific cases or case types, and fail loudly
when provider config/credentials are missing.

### Intervention

Added:

- `tests/rag_quality/answer_eval.py`
- `tests/rag_quality/live_answer_eval.py`
- `tests/rag_quality/test_live_answer_eval.py`

`answer_eval.py` now holds reusable fixture loading, context message assembly,
generation-envelope construction, and groundedness/citation assertions.

`live_answer_eval.py` runs opt-in live evals:

```text
venv/bin/python -m tests.rag_quality.live_answer_eval --case negative_query_absent
venv/bin/python -m tests.rag_quality.live_answer_eval --limit 2
venv/bin/python -m tests.rag_quality.live_answer_eval --all
```

It calls `ai_chat_stream()` with fixture context/user messages and validates the
final assistant answer using the same deterministic checks as the normal eval
suite.

### Eval

```text
venv/bin/pytest tests/rag_quality/test_answer_grounding_eval.py -q
41 passed in 3.86s

venv/bin/pytest tests/rag_quality -q
48 passed in 3.85s

venv/bin/ruff check tests/rag_quality/answer_eval.py tests/rag_quality/test_answer_grounding_eval.py tests/rag_quality/live_answer_eval.py tests/rag_quality/test_live_answer_eval.py
All checks passed!
```

CLI help was also verified:

```text
venv/bin/python -m tests.rag_quality.live_answer_eval --help
```

The first `--help` attempt inside the sandbox failed during OpenMP shared-memory
initialization while importing the ML/guardrails stack. Re-running the same
local help command outside the sandbox succeeded and printed the expected CLI
usage. No live API call was made.

### Review

Decision: keep.

Reasoning:

- It completes the path from deterministic fixture evals to an explicit bounded
  live-answer command without making CI/local tests flaky.
- It reuses the same groundedness and citation checks, so live and captured
  results are judged by the same contract.
- It keeps all work local and avoids server actions, Redis, deploys, and
  reindexing.

Remaining gap:

- Live eval results have not been run against a real provider in this loop
  because that requires credentials/network and can incur cost. The command is
  now available for that explicit local run.

## Loop 14: Fixture Retrieval Source Precision

### Measure

`docs/20_rag_quality_improvement_prompt.md` requires eval coverage for retrieved
chunk relevance, source precision, and noisy source context. Previous loops
validated context/answer behavior after snippets were selected, but did not
apply the answer fixture to retrieval ranking itself.

Added a fixture-level retrieval eval with expected source snippets plus noisy
distractors. Initial result:

```text
venv/bin/pytest tests/rag_quality/test_retrieval_fixture_eval.py -q
4 failed, 7 passed
```

Failing classes:

- broad page summary;
- multi-section enumeration;
- noisy source context;
- negative absent-answer source discovery.

Score inspection showed the same pattern: a `section_summary` distractor that
echoed the user query text could outrank the expected source. Two root causes
were visible:

- generic query/action words such as `the`, `list`, `summarize`, and `what`
  counted as lexical overlap;
- document title was not part of the rerank field boosts, so source titles like
  `Onboarding` and `March release notes` did not help;
- summary/section-summary snippets that simply repeated the raw query were not
  penalized.

### Hypothesis

Source precision should improve if retrieval:

- filters generic stop/action terms before overlap scoring;
- uses source title as a ranking signal;
- penalizes summary-like snippets that repeat the raw query without adding
  answer evidence.

### Intervention

Updated `vchat/views/chat/ctx.py`:

- added `LEXICAL_STOP_TERMS`;
- capped `queryprofile()` lexical terms at `MAX_LEXICAL_TERMS`;
- added title field boost in `RERANK_FIELD_WEIGHTS`;
- added `RERANK_QUERY_ECHO_PENALTY` for `summary` and `section_summary` snippets
  containing the normalized raw query.

Added/updated tests:

- `tests/rag_quality/test_retrieval_fixture_eval.py`;
- `tests/test_retrieval_ctx.py`.

### Eval

```text
venv/bin/pytest tests/rag_quality/test_retrieval_fixture_eval.py -q
11 passed in 3.87s

venv/bin/pytest tests/test_retrieval_ctx.py -q
50 passed in 3.86s

venv/bin/pytest tests/rag_quality -q
59 passed in 3.93s

venv/bin/ruff check vchat/views/chat/ctx.py tests/test_retrieval_ctx.py tests/rag_quality/test_retrieval_fixture_eval.py
All checks passed!
```

### Review

Decision: keep.

Reasoning:

- The intervention is driven by a failing fixture-level source precision eval,
  not by subjective inspection.
- It directly addresses noisy context and query-echo failure modes from the
  prompt.
- It improves ranking using source metadata already present in snippets.

Remaining gap:

- The retrieval fixture is still synthetic and deterministic. A future loop
  should run the same source precision checks against a small local DB slice
  after local embeddings/chunks are regenerated for selected pages.

## Loop 15: Read-Only Local DB Slice Eval

### Measure

Loop 14 covered source precision with synthetic fixture snippets. The remaining
gap was a local DB slice that reads real pages, applies current chunk/materialize
policy in memory, and optionally ranks generated chunks for a query without
writing to the database or launching any indexing jobs.

### Hypothesis

A read-only local DB runner can catch mismatches between fixture assumptions and
real page rows. It should use the actual materialization policy by default so
metadata-only rules are evaluated, not bypassed.

### Intervention

Added:

- `tests/rag_quality/local_db_slice_eval.py`
- `tests/rag_quality/test_local_db_slice_eval.py`

Runner examples:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval --page-id 46902 --page-id 50073 --limit 2
venv/bin/python -m tests.rag_quality.local_db_slice_eval --page-id 50073 --query CHANGELOG --deterministic-rerank --top-k 2
```

Behavior:

- reads local DB pages only;
- defaults to `--mode materialize`, using `materialize_page_chunks()` with a fake
  session so no DB writes occur;
- supports `--mode chunker` for low-level chunker diagnostics;
- supports optional query ranking over generated chunks;
- normalizes SQLAlchemy async DB URIs for `asyncpg`;
- normalizes JSONB `meta` values returned by asyncpg.

### Eval

Unit tests:

```text
venv/bin/pytest tests/rag_quality/test_local_db_slice_eval.py -q
7 passed in 5.55s

venv/bin/ruff check tests/rag_quality/local_db_slice_eval.py tests/rag_quality/test_local_db_slice_eval.py
All checks passed!
```

Read-only local DB slice:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval --page-id 46902 --page-id 50073 --limit 2
```

Result:

| Page | URL | Content chars | Chunks | Kind counts |
| ---: | --- | ---: | ---: | --- |
| 46902 | `https://ksp.vbudushee.ru/public/home/documents` | 97,691 | 3 | `entity_projection=1`, `summary=1`, `text=1` |
| 50073 | `https://www.pylot.me/assets/vendor/codemirror/CHANGELOG/` | 77,320 | 1 | `file_summary=1` |

Read-only local DB retrieval slice:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval --page-id 50073 --query CHANGELOG --deterministic-rerank --top-k 2
```

Result: the selected top chunk was the metadata-only `file_summary` for
`https://www.pylot.me/assets/vendor/codemirror/CHANGELOG/` with `citation_id=0`.

The first sandboxed command attempts hit OpenMP shared-memory initialization
errors before execution. Re-running the same local read-only commands outside
the sandbox succeeded. No writes, server actions, Redis actions, deployment, or
reindexing were performed.

### Review

Decision: keep.

Reasoning:

- This closes the gap between synthetic fixture evals and real local DB rows.
- It uses the actual materialization policy by default; the first draft used raw
  `chunk_document_text()` and incorrectly showed the vendor page as 613 chunks,
  which the runner then exposed and corrected.
- It keeps DB work read-only and local.

Remaining gap:

- This still does not regenerate embeddings. A later loop can run a bounded
  local embedding/retrieval slice for a handful of pages if local model/runtime
  cost is acceptable.

## Loop 16: Bounded Local Embedding Slice

### Measure

Loop 15 added a read-only local DB slice that materializes chunks in memory and
can run deterministic rerank scoring. It still did not load the local embedding
model or evaluate vector similarity over generated chunks.

Measured gap:

- no local embedding model call in RAG quality slice;
- no in-memory query/chunk vector similarity;
- no way to validate that metadata-only chunks remain retrievable by embedding
  similarity without writing vectors to the database.

### Hypothesis

An explicit `--embed-query` option on the local DB slice runner can exercise the
local embedding model over a bounded set of generated chunks while preserving the
no-write/no-reindex constraint.

### Intervention

Extended `tests/rag_quality/local_db_slice_eval.py`:

- added `--embed-query`;
- loads the local embedding model only when that flag is provided;
- embeds `[query, *chunk_texts]` in memory;
- ranks chunks by cosine similarity;
- prints `embedding_top_chunks`;
- does not write embeddings to the database.

Added unit coverage in `tests/rag_quality/test_local_db_slice_eval.py` using a
fake embedder, so normal tests do not load the real model.

### Eval

Unit tests:

```text
venv/bin/pytest tests/rag_quality/test_local_db_slice_eval.py -q
9 passed in 5.82s

venv/bin/ruff check tests/rag_quality/local_db_slice_eval.py tests/rag_quality/test_local_db_slice_eval.py
All checks passed!
```

Existing non-embedding local DB slice still works:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval --page-id 50073 --query CHANGELOG --deterministic-rerank --top-k 1
```

Result: top chunk is the metadata-only `file_summary` for the CodeMirror
CHANGELOG page.

Bounded local embedding slice:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval --page-id 50073 --embed-query CHANGELOG --top-k 1
```

Result:

| Page | Query | Top kind | Top score |
| ---: | --- | --- | ---: |
| 50073 | `CHANGELOG` | `file_summary` | 0.63165 |

The command read one local DB page, materialized chunks in memory, loaded the
local embedding model, computed cosine similarity in memory, and did not write
vectors or trigger reindexing.

### Review

Decision: keep.

Reasoning:

- This completes a bounded local embedding/retrieval slice while respecting the
  no server/no Redis/no reindex constraints.
- Metadata-only document discovery is validated through both rerank and
  embedding-similarity paths.
- The real model is only loaded behind an explicit CLI option; normal tests use
  a fake embedder.

Remaining gap:

- This is still a one-page embedding slice. A later loop can expand to a small
  multi-page fixture with expected source precision metrics, while keeping the
  run bounded and local.

## Loop 17: Multi-Page Embedding Source Precision Slice

### Measure

Loop 16 validated embedding similarity on a single page. That did not measure
source precision across pages: a query should rank the expected source above
other materialized local pages.

### Hypothesis

The local DB slice runner should support global embedding ranking over all
selected pages and report whether an expected source URI appears in the top-k
results.

### Intervention

Extended `tests/rag_quality/local_db_slice_eval.py`:

- added global embedding ranking across all selected pages;
- added `--expected-uri`;
- reports `embedding_global_top_chunks`;
- reports `expected_uri_hit`.

Added fake-embedder coverage in `tests/rag_quality/test_local_db_slice_eval.py`
so normal tests still avoid loading the real embedding model.

### Eval

Unit tests:

```text
venv/bin/pytest tests/rag_quality/test_local_db_slice_eval.py -q
10 passed in 4.54s

venv/bin/ruff check tests/rag_quality/local_db_slice_eval.py tests/rag_quality/test_local_db_slice_eval.py
All checks passed!
```

Bounded multi-page local embedding slice:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval \
  --page-id 46902 \
  --page-id 50073 \
  --embed-query CHANGELOG \
  --expected-uri https://www.pylot.me/assets/vendor/codemirror/CHANGELOG/ \
  --top-k 3
```

Result:

| Rank | Page | Kind | Score | URL |
| ---: | ---: | --- | ---: | --- |
| 1 | 50073 | `file_summary` | 0.631650 | `https://www.pylot.me/assets/vendor/codemirror/CHANGELOG/` |
| 2 | 46902 | `summary` | 0.379128 | `https://ksp.vbudushee.ru/public/home/documents` |
| 3 | 46902 | `text` | 0.357467 | `https://ksp.vbudushee.ru/public/home/documents` |

`expected_uri_hit=true`.

No vectors were written to the DB; all chunks and embeddings were generated
in-memory from a two-page local slice.

### Review

Decision: keep.

Reasoning:

- The runner now measures source precision across multiple real local pages,
  not only within one page.
- The metadata-only `file_summary` source wins both rerank and embedding
  similarity on the tested query.
- Normal tests still use fake embeddings; the real model is only loaded by the
  explicit local CLI command.

New measured gap:

- Page `46902` materialized chunks still include a leading JSON cookie/config
  fragment such as `{"isActive":false,"cookieKey":null}` after HTML
  normalization. A later chunking loop should measure and remove this kind of
  non-answer UI/config noise without hiding useful structured content.

## Loop 18: Drop HTML UI Config JSON Lines

### Measure

Loop 17 exposed a concrete defect in the real local DB slice: page `46902`
(`https://ksp.vbudushee.ru/public/home/documents`) materialized to only three
chunks after HTML normalization, but the text still started with a standalone
UI/cookie config JSON fragment:

```text
{"isActive":false,"cookieKey":null}
```

That fragment is not answer-bearing site content, yet it polluted previews,
entity extraction, and embedding input.

### Hypothesis

Remove only standalone short UI/config JSON object lines produced by full-HTML
text extraction. Do not apply the rule to plain-text documents, because JSON can
be useful indexed content or an example in documentation.

### Intervention

Updated `jobs/embedder/chunking.py`:

- added a narrow `HTML_UI_CONFIG_JSON_LINE_RE`;
- applied it only inside `normalize_html_document_for_chunking()`, after full
  HTML documents are parsed and converted to visible text;
- preserved non-HTML/plain-text JSON content.

Added tests in `tests/test_embedder_chunking_limits.py`:

- full HTML containing `{"isActive":false,"cookieKey":null}` drops the config
  line and keeps useful page text;
- plain-text JSON with the same keys remains indexable.

### Eval

Unit and lint:

```text
venv/bin/pytest tests/test_embedder_chunking_limits.py -q
17 passed in 0.45s

venv/bin/ruff check jobs/embedder/chunking.py tests/test_embedder_chunking_limits.py
All checks passed!
```

Read-only local DB slice:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval \
  --page-id 46902 \
  --query Документы \
  --deterministic-rerank \
  --top-k 3
```

Result:

| Page | Chunks | Chars | Top kind | Top preview |
| ---: | ---: | ---: | --- | --- |
| 46902 | 3 | 1041 | `text` | `Документы Инструкция по заполнению заявки...` |

The previous JSON config fragment no longer appears in the selected chunk
previews.

Regression check on the previous multi-page embedding source-precision slice:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval \
  --page-id 46902 \
  --page-id 50073 \
  --embed-query CHANGELOG \
  --expected-uri https://www.pylot.me/assets/vendor/codemirror/CHANGELOG/ \
  --top-k 3
```

Result:

| Rank | Page | Kind | Score | URL |
| ---: | ---: | --- | ---: | --- |
| 1 | 50073 | `file_summary` | 0.631650 | `https://www.pylot.me/assets/vendor/codemirror/CHANGELOG/` |
| 2 | 46902 | `summary` | 0.368795 | `https://ksp.vbudushee.ru/public/home/documents` |
| 3 | 46902 | `text` | 0.352645 | `https://ksp.vbudushee.ru/public/home/documents` |

`expected_uri_hit=true`.

### Review

Decision: keep.

Reasoning:

- The intervention removes measured non-answer UI/config noise from full HTML
  chunks without changing plain-text JSON behavior.
- Real local DB materialization now starts page `46902` chunks with useful
  document-list content.
- The existing metadata-only source-precision slice still ranks the expected
  `CHANGELOG` source first.

Remaining gap:

- The current eval loop still covers only a few hand-picked local pages. The
  next chunking loop should add a small corpus-level noise metric, for example
  counting selected chunk previews that begin with machine/UI artifacts across a
  bounded local page sample.

## Loop 19: Size HTML Pages by Chunkable Visible Text

### Measure

Loop 18 suggested adding a corpus-level noise metric. I extended the local DB
slice runner to report `machine_artifact_prefix_chunks` and
`machine_artifact_prefix_ratio`, then ran a bounded local sample:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval --limit 20
```

Before the intervention:

| Pages | Materialized chunks | Machine-artifact chunks | Machine-artifact ratio |
| ---: | ---: | ---: | ---: |
| 20 | 97 | 0 | 0.0 |

The sample exposed a different chunking defect: some non-empty HTML pages with
large raw `content_chars` materialized to zero chunks because the size guard ran
against raw HTML, not against the text that the chunker would actually index.

Concrete example:

| Page | URL | Raw content chars | Raw materialize chunks | Raw chunker chunks |
| ---: | --- | ---: | ---: | ---: |
| 46508 | `https://grant.vbudushee.ru/public/home/documents` | 383955 | 0 | 3 |

The visible normalized text for that page was only 342 chars and contained useful
downloadable document listings.

### Hypothesis

For normal HTML pages, the `embedding_document_max_chars` guard should measure
the chunkable visible text after full-HTML normalization. For non-HTML/plain-text
content this remains the original content. Metadata-only policies still run
before the size guard, so giant CSV/code/vendor assets keep their reduced-index
behavior.

### Intervention

Updated `jobs/crawler/tasks.py`:

- added `_indexable_content_for_size_policy()`;
- added `_document_exceeds_indexable_size_limit()`;
- changed `fetch_page_context()` and `materialize_page_chunks()` to mark
  documents too big only when the chunkable text exceeds the limit.
- changed `fetch_page_context()` so old `status_error=too_big` no longer blocks
  re-indexing when the current chunkable text is within the size limit.

Added regression coverage:

- large raw HTML with a tiny visible body now materializes visible text chunks;
- oversized plain text still marks the page `too_big`.
- old `too_big` full-HTML pages can be fetched for indexing once visible text
  fits the size policy.

Extended `tests/rag_quality/local_db_slice_eval.py`:

- reports per-page and aggregate machine-artifact prefix metrics;
- fake materialize session now supports the existing oversized-page path.

### Eval

Focused tests and lint:

```text
venv/bin/pytest tests/test_embedder_chunking_limits.py -q
19 passed in 0.48s

venv/bin/pytest tests/rag_quality/test_local_db_slice_eval.py -q
13 passed in 4.18s

venv/bin/ruff check jobs/crawler/tasks.py jobs/embedder/chunking.py \
  tests/test_embedder_chunking_limits.py \
  tests/rag_quality/local_db_slice_eval.py \
  tests/rag_quality/test_local_db_slice_eval.py
All checks passed!
```

Real local page eval:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval \
  --page-id 46508 \
  --query Документы \
  --deterministic-rerank \
  --top-k 3
```

Result:

| Page | Mode | Chunks | Chars | Top kind | Top preview |
| ---: | --- | ---: | ---: | --- | --- |
| 46508 | `materialize` | 3 | 820 | `text` | `Документы Вебинар для участников конкурса.pdf...` |

Bounded corpus sample after the intervention:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval --limit 20
```

| Pages | Materialized chunks | Machine-artifact chunks | Machine-artifact ratio |
| ---: | ---: | ---: | ---: |
| 20 | 103 | 0 | 0.0 |

The sample gained chunks for previously zero-chunk HTML pages whose visible text
was below the size limit.

### Review

Decision: keep.

Reasoning:

- The previous guard treated removable HTML chrome/script payload as if it were
  answer-bearing content and dropped pages that had small useful visible text.
- The intervention preserves the existing fail-fast oversized behavior for
  genuinely huge chunkable text.
- Metadata-only document policies remain ahead of the size guard.

New measured gap:

- Page `46505` (`/identity/account/login`) now also materializes a small visible
  text chunk set. Auth/login pages are probably low-value for final answer
  context and should be measured separately for a reduced-index or exclusion
  policy instead of relying on raw HTML size to suppress them.

## Loop 20: Measure Auth-Like Pages Before Policy Changes

### Measure

Loop 19 exposed a possible quality risk: auth/login pages can now materialize
small visible-text chunks after the HTML size guard was fixed.

Local corpus count:

```text
psql postgresql://xen@localhost:5432/vchat -P pager=off -c "
select count(*) as auth_like_pages, coalesce(sum(length(content)),0) as content_chars
from page
where uri ~* '/(login|signin|sign-in|identity|account|auth|register|registration)(/|$)'
   or title ~* '^(вход|регистрация|login|sign in|sign-in|registration)$';
..."
```

Result:

| Auth-like pages | Content chars |
| ---: | ---: |
| 16 | 455251 |

Most auth-like URLs were already excluded by robots or had `low_content`. The
two substantial pages were login pages:

| Page | URL | Status error | Content chars |
| ---: | --- | --- | ---: |
| 46505 | `https://grant.vbudushee.ru/identity/account/login` | `too_big` | 369318 |
| 46900 | `https://ksp.vbudushee.ru/identity/account/login` | none | 85791 |

Bounded materialization/rerank sample:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval \
  --page-id 46505 \
  --page-id 46900 \
  --page-id 46911 \
  --page-id 46817 \
  --query Регистрация \
  --deterministic-rerank \
  --top-k 3
```

Result:

| Pages | Chunks | Auth-like pages | Auth-like chunks | Zero-chunk nonempty pages |
| ---: | ---: | ---: | ---: | ---: |
| 4 | 5 | 4 | 5 | 2 |

Top login-page chunks contained short user-visible text such as:

```text
Вход Регистрация Выслать письмо для активации аккаунта
```

### Hypothesis

Do not add a production exclusion or metadata-only policy for auth/login pages
yet. The measured chunks are small, readable, and may be useful for questions
about registration, login, account activation, or personal-data consent. A broad
auth-page policy would risk deleting discoverable product facts without an eval
showing answer-quality harm.

### Intervention

Extended `tests/rag_quality/local_db_slice_eval.py` with measurement-only
quality counters:

- per-page `auth_like_page`;
- per-page `auth_like_chunks`;
- per-page `zero_chunk_nonempty_page`;
- aggregate `quality.auth_like_pages`;
- aggregate `quality.auth_like_chunks`;
- aggregate `quality.zero_chunk_nonempty_pages`.

Moved `vchat.embeddings.load_embedding_model` import into the explicit
`--embed-query` path. This keeps non-embedding eval tests from importing native
torch/spacy dependencies, while preserving fail-fast behavior for embedding evals
that actually request the model.

### Eval

Focused tests:

```text
venv/bin/pytest tests/rag_quality/test_local_db_slice_eval.py -q
15 passed in 5.75s
```

Auth-like local eval:

| Pages | Chunks | Machine-artifact chunks | Auth-like pages | Auth-like chunks | Zero-chunk nonempty pages |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 5 | 0 | 4 | 5 | 2 |

Bounded corpus sample:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval --limit 20
```

| Pages | Chunks | Machine-artifact chunks | Auth-like pages | Auth-like chunks | Zero-chunk nonempty pages |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 20 | 103 | 0 | 1 | 3 | 2 |

Embedding regression after moving the import:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval \
  --page-id 46902 \
  --page-id 50073 \
  --embed-query CHANGELOG \
  --expected-uri https://www.pylot.me/assets/vendor/codemirror/CHANGELOG/ \
  --top-k 3
```

Result: `expected_uri_hit=true`; the CodeMirror `file_summary` remains rank 1
with score `0.631650`.

### Review

Decision: keep eval instrumentation; reject auth-page production policy for now.

Reasoning:

- The measured auth-like chunks are few and not machine noise.
- A broad exclusion could remove useful account/registration facts.
- The new counters make auth-like context exposure and zero-chunk nonempty pages
  visible in future bounded samples.

New measured gap:

- The 20-page sample still has two zero-chunk nonempty pages:
  `https://teacher.vbudushee.ru/` and a `301 Moved Permanently` page. The next
  loop should inspect whether these are true no-content/redirect cases or
  chunker/filtering misses.

## Loop 21: Metadata-Only for Empty Visible HTML With Useful Title

### Measure

Loop 20 exposed two `zero_chunk_nonempty_pages` in the 20-page local sample.

Raw chunker/materialize comparison:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval \
  --page-id 46502 \
  --page-id 46503 \
  --mode chunker \
  --query Школа \
  --deterministic-rerank \
  --top-k 3
```

Result: both pages produced zero chunks.

Normalized visible text inspection:

| Page | URL | Title | Raw chars | Normalized visible chars | Interpretation |
| ---: | --- | --- | ---: | ---: | --- |
| 46502 | `https://teacher.vbudushee.ru/` | `Школа возможностей` | 2065 | 0 | JS/app shell with useful title |
| 46503 | `https://lpconference.vbudushee.ru/` | `301 Moved Permanently` | 23 | 23 | Redirect/status page |

### Hypothesis

A page with no visible body text but a normal title and URL should remain
discoverable as metadata-only. Redirect/error/status titles should remain
zero-chunk because they are not answer-bearing source content.

### Intervention

Updated `jobs/crawler/tasks.py`:

- added `empty_visible_text` metadata-only reason;
- detect empty visible text through the same HTML normalization used by the
  chunker;
- skip `empty_visible_text` metadata-only for low-value HTTP status titles such
  as `301 Moved Permanently`;
- avoid raw HTML preview in the metadata-only chunk for `empty_visible_text`.

Added tests in `tests/test_embedder_chunking_limits.py`:

- empty visible HTML with title `Школа возможностей` creates one
  `file_summary` chunk with `index_policy_reason=empty_visible_text`;
- redirect title `301 Moved Permanently` remains zero-chunk.

### Eval

Focused tests:

```text
venv/bin/pytest tests/test_embedder_chunking_limits.py -q
21 passed in 0.48s

venv/bin/ruff check jobs/crawler/tasks.py tests/test_embedder_chunking_limits.py
All checks passed!
```

Real local page eval:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval \
  --page-id 46502 \
  --page-id 46503 \
  --query Школа \
  --deterministic-rerank \
  --top-k 3
```

Result:

| Page | Chunks | Kind | Zero-chunk nonempty | Top preview |
| ---: | ---: | --- | --- | --- |
| 46502 | 1 | `file_summary` | false | `Document indexed as metadata only. Title: Школа возможностей...` |
| 46503 | 0 | none | true | none |

Bounded 20-page sample:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval --limit 20
```

| Pages | Chunks | Machine-artifact chunks | Auth-like pages | Auth-like chunks | Zero-chunk nonempty pages |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 20 | 104 | 0 | 1 | 3 | 1 |

Compared to Loop 20, the sample gained one useful metadata-only source and
reduced zero-chunk nonempty pages from `2` to `1`. The remaining zero-chunk page
is the redirect/status page.

### Review

Decision: keep.

Reasoning:

- The intervention improves source discoverability for an otherwise invisible
  JS/app-shell page without pretending it has body facts.
- Redirect/status pages remain out of answer context.
- The metadata-only chunk has title, URL, content type, and policy reason, but
  avoids raw HTML preview.

Remaining gap:

- Empty visible JS/app-shell pages may need richer metadata later if crawler
  extraction can capture OpenGraph/meta description or structured data. Current
  change deliberately stops at discoverability.

## Loop 22: Answer Eval for Empty Visible Metadata-Only Pages

### Measure

Loop 21 added `empty_visible_text` metadata-only chunks. The existing answer eval
already covered a CSV metadata-only source, but it did not cover JS/app-shell
HTML pages whose only safe answer-bearing facts are title, URL, type, and policy
reason.

### Hypothesis

The answer pipeline should be allowed to confirm that an empty-visible HTML page
exists, cite the metadata-only source, and avoid inventing body facts that are
not present in indexed context.

### Intervention

Added `empty_visible_html_metadata_only` to
`tests/fixtures/rag_quality/answer_grounding_cases.json`:

- source kind: `file_summary`;
- title: `Школа возможностей`;
- URL: `https://teacher.vbudushee.ru/`;
- policy reason: `empty_visible_text`;
- forbidden claims: `course schedule`, `application deadline`,
  `teacher requirements`.

No production code change was needed in this loop.

### Eval

Focused answer/retrieval eval:

```text
venv/bin/pytest \
  tests/rag_quality/test_answer_grounding_eval.py \
  tests/rag_quality/test_retrieval_fixture_eval.py \
  -q
57 passed in 4.19s
```

Full RAG quality eval:

```text
venv/bin/pytest tests/rag_quality -q
79 passed in 4.26s
```

### Review

Decision: keep.

Reasoning:

- The eval now locks the desired answer contract for `empty_visible_text`
  metadata-only chunks.
- It confirms that `file_summary` can support source discoverability answers
  without allowing unsupported body facts.
- Retrieval fixture eval keeps the expected metadata-only source above generic
  distractors.

Remaining gap:

- This is deterministic fixture coverage. A later live/local answer eval should
  include a real generated answer for metadata-only page discoverability once
  live-answer runs are desired for this slice.

## Loop 23: Global Query Hit@K in Local DB Slice Eval

### Measure

The local DB slice runner could already materialize chunks and rank chunks per
page. It could also compute global `expected_uri_hit` for embedding similarity.
However, normal query/rerank evals did not have a global hit@k metric across
multiple local pages. That made it weaker for measuring local end-to-end
chunking → retrieval → context selection behavior.

Concrete target slice:

- page `46502`: metadata-only `file_summary` for `Школа возможностей`;
- page `46503`: redirect/status page with zero chunks;
- query: `Школа возможностей`;
- expected URI: `https://teacher.vbudushee.ru/`.

### Hypothesis

The local slice runner should report global query-selected snippets and
`query_expected_uri_hit`, just like embedding eval reports
`expected_uri_hit`. This lets small local experiments verify source precision
without loading the embedding model or calling an LLM.

### Intervention

Extended `tests/rag_quality/local_db_slice_eval.py`:

- added `rank_global_chunks()`;
- added top-level `query_global_top_chunks`;
- added top-level `query_expected_uri_hit`;
- kept existing per-page `top_chunks` output.

Added unit coverage in `tests/rag_quality/test_local_db_slice_eval.py` for
global query ranking across pages.

### Eval

Focused tests and lint:

```text
venv/bin/pytest tests/rag_quality/test_local_db_slice_eval.py -q
16 passed in 6.27s

venv/bin/ruff check \
  tests/rag_quality/local_db_slice_eval.py \
  tests/rag_quality/test_local_db_slice_eval.py
All checks passed!
```

Real local DB query slice:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval \
  --page-id 46502 \
  --page-id 46503 \
  --query "Школа возможностей" \
  --expected-uri https://teacher.vbudushee.ru/ \
  --deterministic-rerank \
  --top-k 3
```

Result:

| Metric | Value |
| --- | --- |
| `query_expected_uri_hit` | `true` |
| Global rank 1 URI | `https://teacher.vbudushee.ru/` |
| Global rank 1 kind | `file_summary` |
| Machine-artifact chunks | `0` |
| Zero-chunk nonempty pages | `1` |

### Review

Decision: keep.

Reasoning:

- The runner now measures source precision for normal query/rerank retrieval
  over multi-page local slices.
- The metadata-only page is discoverable without needing live LLM calls or
  embedding model loading.
- The remaining redirect page is visible in `zero_chunk_nonempty_pages` but does
  not enter context.

Remaining gap:

- This still validates context-source selection, not the final generated text.
  Live answer eval remains opt-in because it uses an external model provider.

## Loop 24: Negative Answer for Unsupported Metadata-Only Body Facts

### Measure

Loop 22 confirmed that metadata-only `file_summary` chunks can support
discoverability answers. It did not cover the opposite behavior: when a user asks
for a body fact that a metadata-only source does not contain, the answer should
say the fact was not found instead of citing the metadata-only source as proof.

Measured missing case:

- source: `https://teacher.vbudushee.ru/`;
- chunk kind: `file_summary`;
- policy reason: `empty_visible_text`;
- user asks for an application deadline;
- metadata contains title and URL only, not deadline facts.

### Hypothesis

Add deterministic answer/retrieval coverage for unsupported body facts on
metadata-only sources. This should require a negative answer with no citations,
while still allowing the metadata-only source to appear in context.

### Intervention

Added `metadata_only_absent_body_fact` to
`tests/fixtures/rag_quality/answer_grounding_cases.json`:

- `case_type`: `negative_query_absent`;
- query: `What is the application deadline for Школа возможностей?`;
- answer: says the deadline was not found in indexed sources;
- source: `file_summary` for `https://teacher.vbudushee.ru/`;
- `citation_required=false`;
- `negative_answer_required=true`;
- forbidden claims include invented deadline wording and dates.

Adjusted `tests/rag_quality/test_retrieval_fixture_eval.py` so negative-case
query terms are derived from the case query and its forbidden claims instead of
being hard-coded to the original iOS-app fixture.

Updated `tests/rag_quality/test_live_answer_eval.py` because selecting by
`case_type=negative_query_absent` now returns two negative cases.

### Eval

Focused answer/retrieval eval:

```text
venv/bin/pytest \
  tests/rag_quality/test_answer_grounding_eval.py \
  tests/rag_quality/test_retrieval_fixture_eval.py \
  -q
62 passed in 4.03s
```

Full RAG quality eval:

```text
venv/bin/pytest tests/rag_quality -q
85 passed in 4.88s
```

Note: one immediate `tests/rag_quality` run aborted inside native
torch/spacy/guardrails imports; the repeat passed. This matches the existing
local native-import instability observed in earlier loops and was not an
assertion failure.

### Review

Decision: keep.

Reasoning:

- The eval now distinguishes metadata-only discoverability from unsupported body
  fact answering.
- Negative answers over metadata-only context must not cite the metadata-only
  source as evidence for absent body facts.
- The retrieval fixture remains scalable for multiple negative cases instead of
  relying on a hard-coded iOS query term.

Remaining gap:

- Live generated-answer validation for this negative metadata-only case remains
  opt-in because it requires an external model provider.

## Loop 25: Context Assembly Summary in Local DB Slice Eval

### Measure

Loop 23 added global query hit@k for local DB slices. It proved source selection
across pages, but it still did not expose the final context payload produced by
the production context assembly path. That left a gap for citation-id alignment:
retrieval could be correct while context JSON could still lose or renumber
sources incorrectly.

### Hypothesis

The local DB slice runner should summarize the production
`build_context_from_snippets()` payload for global query-selected snippets. This
keeps the eval bounded and local while covering chunking → retrieval → context
assembly.

### Intervention

Extended `tests/rag_quality/local_db_slice_eval.py`:

- added reusable local `EvalProvider` / `EvalModel`;
- added `select_global_snippets()`;
- added `context_summary_from_snippets()`;
- added top-level `query_context` with:
  - `snippet_count`;
  - `source_count`;
  - `citation_ids`;
  - `uris`;
  - `kinds`.

Added unit coverage in `tests/rag_quality/test_local_db_slice_eval.py` to verify
metadata-only `file_summary` snippets preserve citation IDs, URI, and kind in
the context summary.

### Eval

Focused tests and lint:

```text
venv/bin/pytest tests/rag_quality/test_local_db_slice_eval.py -q
17 passed in 5.08s

venv/bin/ruff check \
  tests/rag_quality/local_db_slice_eval.py \
  tests/rag_quality/test_local_db_slice_eval.py
All checks passed!
```

Real local DB context slice:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval \
  --page-id 46502 \
  --page-id 46503 \
  --query "Школа возможностей" \
  --expected-uri https://teacher.vbudushee.ru/ \
  --deterministic-rerank \
  --top-k 3
```

Result:

| Metric | Value |
| --- | --- |
| `query_expected_uri_hit` | `true` |
| `query_context.snippet_count` | `1` |
| `query_context.source_count` | `1` |
| `query_context.citation_ids` | `[0]` |
| `query_context.uris` | `[https://teacher.vbudushee.ru/]` |
| `query_context.kinds` | `[file_summary]` |

### Review

Decision: keep.

Reasoning:

- The runner now checks source selection and context citation alignment on real
  local materialized pages.
- The metadata-only source survives production context assembly as citation `0`.
- The redirect/status page remains absent from context while still visible in
  `zero_chunk_nonempty_pages`.

Remaining gap:

- The eval summarizes context payload rather than storing full JSON. That is
  enough for citation/source alignment, but a later debug mode could expose full
  payload when investigating failures.

## Loop 26: Large Downloadable Document Metadata Policy

### Measure

The task requires document/file metadata chunks for downloadable documents, but
the local DB did not contain a useful real large PDF/Word/PowerPoint sample.

Strict extension/content-type query:

```sql
select count(*) as doc_like_pages, coalesce(sum(length(content)),0) as content_chars
from page
where uri ~* '\.(pdf|docx?|pptx?|xlsx?)(\?|$)'
   or raw_content_type ~* '(pdf|word|powerpoint|excel|spreadsheet|officedocument)';
```

Result:

| Metric | Value |
| --- | ---: |
| `doc_like_pages` | 1 |
| `content_chars` | 23 |

The only hit was:

| Page | URL | Title | Status | Content type | Chars |
| --- | --- | --- | --- | --- | ---: |
| 46913 | `http://ksp.vbudushee.ru/public/api/v1/file/get-document?filename=adce7887-ccf0-44a5-9ec1-ff581bd0df23.docx` | `301 Moved Permanently` | `low_content` | `text/html` | 23 |

A broader URL/meta query for `file`, `get-document`, `download`, `filename`,
and document type terms found 174 pages / 19,987,162 chars, but the top rows
were mostly normal HTML pages such as `/documents`, library pages, and large
SPA pages. That broad class is not safe evidence for a downloadable-document
policy.

### Hypothesis

The crawler should have a guarded metadata-only policy for explicit large
downloadable documents even if the current local DB only gives a redirect-like
negative sample. The policy should:

- require a strong document hint: file extension or document MIME type;
- only trigger when extracted content or raw file size is large;
- keep small extracted PDF/DOC/PPT/XLS content fully indexable;
- not infer a document from weak API URL terms such as `filename=` when the
  fetched content is just low-value HTML.

### Intervention

Added `large_downloadable_document` to `jobs/crawler/tasks.py`.

The rule is intentionally narrow:

- extensions: `.pdf`, `.doc`, `.docx`, `.ppt`, `.pptx`, `.xls`, `.xlsx`;
- MIME/content-type terms: `pdf`, `msword`, `powerpoint`, `excel`,
  `spreadsheet`, `officedocument`;
- size gate: extracted content exceeds `EMBEDDING_DOCUMENT_MAX_CHARS` or raw
  size exceeds `metadata_only_raw_size_min_bytes`.

Added regression coverage:

- a large PDF becomes exactly one `file_summary` chunk with
  `index_policy_reason=large_downloadable_document`;
- a small extracted PDF remains normal full-text indexed content;
- the fixture eval now includes `large_downloadable_document_is_metadata_only`.

### Eval

Focused tests:

```text
venv/bin/pytest \
  tests/test_embedder_chunking_limits.py \
  tests/rag_quality/test_chunking_policy_eval.py \
  -q
28 passed in 0.64s
```

Lint:

```text
venv/bin/ruff check \
  jobs/crawler/tasks.py \
  tests/test_embedder_chunking_limits.py \
  tests/rag_quality/test_chunking_policy_eval.py
All checks passed!
```

RAG quality suite:

```text
venv/bin/pytest tests/rag_quality -q
87 passed in 6.22s
```

Note: the first `tests/rag_quality` run aborted during the known local native
torch/spacy/guardrails import path; the repeat passed and no pytest assertion
failed.

Full test suite:

```text
venv/bin/pytest -q
582 passed, 2 skipped, 2 warnings in 6.67s
```

### Review

Decision: keep.

Reasoning:

- This closes an explicit document-policy gap from the task without broadening
  weak URL heuristics into false-positive document summaries.
- Large downloadable documents now become discoverable, citation-ready
  `file_summary` chunks instead of being marked only as `too_big`.
- Full content QA remains possible for small, clean extracted documents.

Remaining gap:

- The current local DB has no useful true large downloadable document sample, so
  the impact is validated by guardrail tests rather than a real DB before/after
  chunk-count improvement.

## Loop 27: Persisted Local DB Materialization Slice

### Measure

Most previous loops used in-memory materialization to avoid touching shared
runtime state. After local DB writes were explicitly allowed, measured the
actual persisted chunks for a bounded four-page slice:

- `46502`: empty visible SPA shell (`Школа возможностей`);
- `46508`: grant document-list page previously blocked by `too_big`;
- `46902`: raw HTML document-list page with pathological old chunks;
- `50073`: vendor changelog that should be metadata-only.

Before local materialization:

| Page | Status | Chunks | File summaries | Kinds |
| --- | --- | ---: | ---: | --- |
| 46502 | empty | 3 | 0 | `entity_projection`, `summary`, `text` |
| 46508 | `too_big` | 0 | 0 | empty |
| 46902 | empty | 927 | 0 | `entity_projection`, `summary`, `text` |
| 50073 | empty | 613 | 0 | `entity_projection`, `section_summary`, `text` |

### Hypothesis

Running the current materializer against only these local rows should persist
the same improvements already proven in-memory:

- app-shell page becomes one metadata-only `file_summary`;
- `too_big` document-list page becomes useful visible-text chunks;
- raw HTML document-list page drops from hundreds of chunks to three;
- vendor asset becomes one metadata-only `file_summary`.

### Intervention

Locally ran `materialize_page_chunks()` for pages `46502`, `46508`, `46902`, and
`50073` using the sync `psycopg` driver. This did not schedule embedding jobs,
did not touch Redis, and did not touch any server.

One environment correction was needed: `postgresql://...` made SQLAlchemy select
the old `psycopg2` driver, which is not installed. The project dependency
`psycopg==3.3.2` is installed, so the local eval command used
`postgresql+psycopg://xen@localhost:5432/vchat`.

Materialization result:

```text
46502: 1
46508: 3
46902: 3
50073: 1
```

After local materialization:

| Page | Status | Policy | Reason | Chunks | File summaries | Kinds |
| --- | --- | --- | --- | ---: | ---: | --- |
| 46502 | empty | `metadata_only` | `empty_visible_text` | 1 | 1 | `file_summary` |
| 46508 | empty | empty | empty | 3 | 0 | `entity_projection`, `summary`, `text` |
| 46902 | empty | empty | empty | 3 | 0 | `entity_projection`, `summary`, `text` |
| 50073 | empty | `metadata_only` | `vendor_asset` | 1 | 1 | `file_summary` |

### Eval

Persisted chunk-count impact:

| Page | Before chunks | After chunks | Change |
| --- | ---: | ---: | ---: |
| 46502 | 3 | 1 | -2 |
| 46508 | 0 | 3 | +3 |
| 46902 | 927 | 3 | -924 |
| 50073 | 613 | 1 | -612 |
| Total | 1543 | 8 | -1535 |

Local end-to-end chunking/retrieval/context eval over the same four pages:

```text
venv/bin/python -m tests.rag_quality.local_db_slice_eval \
  --page-id 46502 \
  --page-id 46508 \
  --page-id 46902 \
  --page-id 50073 \
  --query CHANGELOG \
  --expected-uri https://www.pylot.me/assets/vendor/codemirror/CHANGELOG/ \
  --deterministic-rerank \
  --top-k 4
```

Result:

| Metric | Value |
| --- | --- |
| `noise.chunk_count` | `8` |
| `noise.machine_artifact_prefix_chunks` | `0` |
| `quality.zero_chunk_nonempty_pages` | `0` |
| `query_expected_uri_hit` | `true` |
| Global rank 1 | page `50073`, kind `file_summary`, rerank `0.81` |
| `query_context.snippet_count` | `1` |
| `query_context.citation_ids` | `[0]` |
| `query_context.uris` | `[https://www.pylot.me/assets/vendor/codemirror/CHANGELOG/]` |

### Review

Decision: keep.

Reasoning:

- The local DB now demonstrates the same materializer behavior as the fixture
  and in-memory evals on real persisted rows.
- The selected sample shrank from 1,543 stored chunks to 8 while also recovering
  a previously zero-chunk `too_big` document-list page.
- Retrieval/context still selects the expected metadata-only vendor source with
  stable citation ID alignment.

Remaining gap:

- Embeddings were not written for this DB slice in this loop. The existing
  in-memory embedding evals cover source precision, but persisted embedding
  rows remain intentionally untouched unless a later local-only embedding write
  loop is needed.

## Loop 28: Answer Guardrail for Large PDF Metadata-Only Sources

### Measure

Loop 26 added chunking/materialization coverage for large downloadable
documents, but the answer fixture still only covered metadata-only CSV and empty
visible HTML sources. That left the new `large_downloadable_document` reason
uncovered at the context/generation envelope layer.

### Hypothesis

A large PDF `file_summary` should support a grounded document-discovery answer:
the assistant may say the PDF exists and cite it, but must not invent detailed
body facts beyond the metadata-only summary.

### Intervention

Added `large_pdf_metadata_only` to
`tests/fixtures/rag_quality/answer_grounding_cases.json`:

- `case_type=downloadable_document_query`;
- source kind `file_summary`;
- source reason `large_downloadable_document`;
- answer cites the PDF metadata-only source;
- forbidden claims prevent full-body/detail hallucination such as application
  deadlines or complete PDF contents.

### Eval

Focused answer grounding:

```text
venv/bin/pytest tests/rag_quality/test_answer_grounding_eval.py -q
53 passed in 5.53s
```

Full RAG quality suite:

```text
venv/bin/pytest tests/rag_quality -q
92 passed in 5.47s
```

Full test suite:

```text
venv/bin/pytest -q
587 passed, 2 skipped, 2 warnings in 6.36s
```

### Review

Decision: keep.

Reasoning:

- The large downloadable document policy is now covered from chunking policy
  through answer context and streamed response envelope.
- The fixture preserves the intended product distinction: metadata-only sources
  can prove existence and URL/title/type, not unsupported body facts.

Remaining gap:

- This is still a deterministic fixture answer, not a live model-generated
  answer. Live answer validation remains opt-in because it requires an external
  model provider.

## Loop 29: Final Recommendation Deliverable

### Measure

`docs/20_rag_quality_improvement_prompt.md` requires a final recommendation
document covering:

- what changed;
- what improved;
- what did not improve;
- remaining risks;
- next engineering steps.

Before this loop, `docs/21_rag_chunking_quality_loop.md` contained detailed
loop evidence, but there was no standalone synthesis document for review and
rollout planning.

### Hypothesis

A concise recommendation document should make the current local result
reviewable without requiring a reader to reconstruct the whole Ralph loop from
29 sections.

### Intervention

Added `docs/22_rag_quality_recommendation.md` with:

- executive summary;
- baseline evidence;
- implemented changes grouped by chunking, indexing policy, retrieval/context,
  and answer layer;
- measured impact tables;
- test/eval coverage;
- what did not improve yet;
- remaining risks;
- recommended next engineering steps.

### Eval

This is a documentation deliverable, so no code path changed. The document uses
the already verified local results from Loops 1-28:

- RAG quality suite: `92 passed`;
- full test suite: `587 passed, 2 skipped, 2 warnings`;
- persisted four-page local DB slice: `1543` chunks before, `8` after;
- `CHANGELOG` retrieval/context hit: rank 1 `file_summary`, citation id `0`.

### Review

Decision: keep.

Reasoning:

- The required final recommendation deliverable now exists as a standalone
  review artifact.
- The document distinguishes proven local improvements from remaining risks and
  next steps instead of overstating completion of the broader RAG roadmap.

## Loop 30: Statistical Dump Heuristic Beyond CSV Hints

### Measure

The Phase 4 CSV/statistical-data requirement calls out more than extension and
content-type checks: table width/row count, numeric density, delimiter patterns,
and low natural-language ratio should also be considered.

The existing local DB has no valuable additional large statistical dump without
CSV hints. A broad probe over large non-CSV pages did find delimiter-like lines,
but the top results were normal HTML articles and SPA pages, for example Pylot
articles and `youcan.vbudushee.ru` pages. This showed that a simple delimiter
regex would be too broad and would incorrectly metadata-only normal content.

### Hypothesis

A safe statistical-dump detector should require multiple independent signals:

- enough sampled non-empty rows;
- a stable delimiter;
- stable wide row width;
- high numeric-cell density;
- low natural-language cell ratio.

This should catch large delimited numeric exports served as `text/plain` or a
generic URL, while avoiding articles that contain a few CSV/code examples.

### Intervention

Added `_looks_like_statistical_dump()` in `jobs/crawler/tasks.py` and wired it
into the existing `csv_statistical_dump` policy.

The detector is bounded to the first 200 non-empty lines and requires:

- at least 50 rows;
- delimiter from comma, tab, semicolon, or pipe;
- at least 6 columns;
- at least 80% row consistency;
- at least 55% numeric cells;
- at most 25% natural-language cells.

Added tests:

- large `text/plain` pipe-delimited numeric export without `.csv` or CSV MIME
  becomes one `file_summary` with `csv_statistical_dump`;
- an article with a few CSV-like lines remains full-text indexed;
- fixture eval includes
  `statistical_dump_without_csv_hint_is_metadata_only`.

### Eval

Focused tests:

```text
venv/bin/pytest \
  tests/test_embedder_chunking_limits.py \
  tests/rag_quality/test_chunking_policy_eval.py \
  -q
31 passed in 0.68s
```

Lint:

```text
venv/bin/ruff check \
  jobs/crawler/tasks.py \
  tests/test_embedder_chunking_limits.py \
  tests/rag_quality/test_chunking_policy_eval.py
All checks passed!
```

### Review

Decision: keep.

Reasoning:

- The CSV/statistical policy now covers delimiter/row-width/numeric-density
  cases required by the prompt, not only extension/content-type hints.
- The detector is intentionally strict because the local measurement showed many
  false-positive-looking HTML pages with a few delimited examples.
- The policy still uses the existing metadata-only reason and file-summary
  behavior, so retrieval/context/answer handling remains unchanged.

## Loop 31: Admin Stats Visibility for Metadata-Only Policy

### Measure

Phase 4 requires metadata-only policy to be visible in admin/debug stats. Before
this loop, policy was written into `Page.meta` and surfaced in chunks/evals, but
the existing `/stats` admin table only showed source document and chunk totals.

### Hypothesis

The existing source statistics table is the smallest appropriate visibility
surface. Adding metadata-only document counts and reason breakdown there makes
policy behavior inspectable without adding new routes or schema.

### Intervention

Updated `vchat/views/projects/views.py`:

- aggregates pages where `meta.index_policy == "metadata_only"`;
- groups by source and `meta.index_policy_reason`;
- adds `metadata_only_count`, `metadata_policy_reasons`, and
  `total_metadata_only_docs` to the stats context.

Updated `vchat/templates/projects/stats.html`:

- adds a `Metadata-only` column to the source data statistics table;
- shows per-source policy reason counts in compact text.

Updated `tests/test_projects_views_heavy.py` to verify source and file-level
metadata-only counts and reason breakdown.

### Eval

Focused admin stats tests:

```text
venv/bin/pytest \
  tests/test_projects_views_heavy.py::test_project_stats_aggregates \
  tests/test_frontend_i18n_admin.py \
  -q
13 passed in 2.54s
```

Lint:

```text
venv/bin/ruff check \
  jobs/crawler/tasks.py \
  jobs/embedder/chunking.py \
  vchat/views/chat/ctx.py \
  vchat/views/chat/views.py \
  vchat/views/projects/views.py \
  tests/test_embedder_chunking_limits.py \
  tests/test_retrieval_ctx.py \
  tests/chat/test_ctx_module.py \
  tests/chat/test_system_prompt_policy.py \
  tests/rag_quality \
  tests/test_projects_views_heavy.py
All checks passed!
```

RAG quality suite:

```text
venv/bin/pytest tests/rag_quality -q
93 passed in 6.42s
```

Full test suite:

```text
venv/bin/pytest -q
590 passed, 2 skipped, 2 warnings in 6.59s
```

### Review

Decision: keep.

Reasoning:

- The metadata-only policy is now visible in the existing admin stats surface.
- The implementation is read-only aggregation over existing JSONB metadata; no
  migration or background job is needed.
- This closes the prompt requirement without introducing a separate debug path.
