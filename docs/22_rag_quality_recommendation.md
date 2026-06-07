# RAG Quality Recommendation

Date: 2026-06-07

Scope: local-only recommendation after the chunking-first Ralph loop documented
in `docs/21_rag_chunking_quality_loop.md`. No server deployment, service
restart, server Redis operation, or server reindex was performed.

## Executive Summary

Keep the implemented local changes and prepare a controlled local-to-server
rollout plan after review.

The loop found a concrete RAG quality failure: raw HTML and downloadable/noisy
file-like content could create hundreds of duplicate or low-value chunks for a
single page. This inflated indexing cost and made retrieval/context noisier.

The implemented changes fix the measured failure class without broad legacy
fallbacks:

- token-aware word-window chunking with bounded overlap;
- full HTML visible-text normalization before chunking;
- metadata-only chunks for giant CSV/statistical dumps, vendor/code assets,
  empty visible app shells, and large downloadable documents;
- size policy based on chunkable visible text for HTML pages;
- citation-ready `file_summary` retrieval/context support;
- stricter context/source alignment and answer prompt policy;
- deterministic RAG quality evals and local DB slice evals.

## Baseline Evidence

Local corpus baseline:

| Metric                 |     Value |
| ---------------------- | --------: |
| Sources                |        38 |
| Pages                  |    11,239 |
| Pages with content     |     3,789 |
| Chunks                 |    39,201 |
| Chunks with embeddings |         0 |
| Max chunks/page        |       927 |
| P99 content chars/page |    98,154 |
| Max content chars/page | 1,033,208 |

Worst measured examples:

|  Page | URL                                                        | Before chunks | Before chunk chars | Problem                                           |
| ----: | ---------------------------------------------------------- | ------------: | -----------------: | ------------------------------------------------- |
| 46902 | `https://ksp.vbudushee.ru/public/home/documents`           |           927 |          6,170,634 | raw HTML treated as plain text                    |
| 46900 | `https://ksp.vbudushee.ru/identity/account/login`          |           904 |          6,244,212 | raw HTML/auth page duplication                    |
| 50073 | `https://www.pylot.me/assets/vendor/codemirror/CHANGELOG/` |           613 |            171,501 | vendor asset fully indexed                        |
| 46508 | `https://grant.vbudushee.ru/public/home/documents`         |             0 |                  0 | useful HTML page blocked as `too_big` by raw size |

## Implemented Changes

### Chunking

- `chunk_text_word_window()` now uses actual embedding token counts and bounded
  token overlap.
- Long tokens are still split by token IDs.
- Overlap is capped to guarantee forward progress and avoid duplicate chunk
  explosions.
- Full HTML documents are normalized to visible text before chunking.
- HTML noise tags and UI config JSON lines are removed from the indexed text.

### Indexing Policy

- `metadata_only` policy is now represented by one `file_summary` chunk.
- Giant CSV/statistical files are metadata-only, including large delimited
  numeric dumps without reliable `.csv` or CSV MIME hints.
- Vendor assets and large code assets are metadata-only.
- Empty visible app-shell pages with useful titles are metadata-only.
- Large downloadable documents are metadata-only when there is a strong file or
  MIME type signal and the extracted/raw size exceeds policy limits.
- Small extracted downloadable documents remain full-text indexable.
- HTML size checks use chunkable visible text rather than raw HTML bytes.
- The admin `/stats` source table shows metadata-only document counts and policy
  reason breakdowns.

### Retrieval and Context

- `file_summary` chunks are quote/context-ready for source discovery.
- Citation IDs are assigned from selected visible snippets and kept aligned with
  context JSON/source payloads.
- Rerank uses title overlap and penalizes query-echo summaries.
- `entity_projection` remains useful for recall, but final context selection is
  more conservative.

### Answer Layer

- The system prompt tells the model to say the answer was not found in indexed
  sources when context does not support the answer.
- The prompt requires citation IDs that appear in supplied context only.
- Fixture evals cover exact facts, FAQ/procedure/table/quote/summary/list
  answers, negative absent answers, noisy context, metadata-only source
  discovery, and large PDF metadata-only discovery.

## Measured Impact

Representative materialization impact:

|  Page |     Before |    After | Change | Result                                        |
| ----: | ---------: | -------: | -----: | --------------------------------------------- |
| 46902 | 927 chunks | 3 chunks |   -924 | useful document-list text retained            |
| 50073 | 613 chunks |  1 chunk |   -612 | vendor asset represented as `file_summary`    |
| 46508 |   0 chunks | 3 chunks |     +3 | useful `too_big` page recovered               |
| 46502 |   3 chunks |  1 chunk |     -2 | empty app shell represented as `file_summary` |

Persisted local DB slice:

| Metric                          |  Value |
| ------------------------------- | -----: |
| Before chunks                   |  1,543 |
| After chunks                    |      8 |
| Delta                           | -1,535 |
| Machine artifact chunks after   |      0 |
| Zero-chunk nonempty pages after |      0 |

Retrieval/context result on the same persisted local slice:

| Query       | Expected source                                            | Result                                        |
| ----------- | ---------------------------------------------------------- | --------------------------------------------- |
| `CHANGELOG` | `https://www.pylot.me/assets/vendor/codemirror/CHANGELOG/` | global rank 1 `file_summary`, citation id `0` |

Embedding source precision was also checked on bounded in-memory slices. For
`CHANGELOG`, the expected metadata-only source ranked first by local embedding
similarity in the measured two-page slice.

## Test and Eval Coverage

Current local verification:

```text
venv/bin/ruff check ...
All checks passed!

venv/bin/pytest tests/rag_quality -q
93 passed

venv/bin/pytest -q
590 passed, 2 skipped, 2 warnings
```

The RAG quality suite includes:

- chunking policy fixture eval;
- retrieval fixture source precision;
- answer groundedness eval;
- context/generation envelope eval;
- fake streamed answer eval;
- opt-in live answer eval command;
- read-only local DB slice eval;
- bounded local embedding source precision eval.

## What Did Not Improve Yet

- Persisted embeddings were not regenerated for the full local corpus.
- The current local DB did not contain a useful real large PDF/Word/PPT sample,
  so large downloadable document behavior is validated by guardrail fixtures,
  not by a real DB before/after sample.
- Full live LLM answer quality remains opt-in because it requires an external
  model provider.
- Structured block segmentation for FAQ, instruction, definition, and table
  row chunks is still future work.
- Embedding worker throughput still needs batch char/token caps; this loop
  reduced bad chunks but did not change queue selection.

## Remaining Risks

- HTML visible-text normalization can remove content if important information is
  rendered only through scripts and not present as HTML text.
- Metadata-only policy intentionally prevents body-level QA for large/noisy
  documents; the answer layer must continue to distinguish source discovery from
  unsupported body facts.
- Some source titles or metadata may be weak, especially for empty visible app
  shells. Those pages are discoverable but may not explain much beyond title,
  URL, type, and policy reason.
- Local DB was changed for a four-page bounded materialization slice. Server
  data remains untouched and still needs a separately approved rollout/reindex
  plan.

## Recommendation

Keep the changes.

The measured impact is large on the failure classes that triggered the work:
pathological raw HTML chunk explosions, vendor/file noise, and useful HTML pages
blocked by raw byte size. The changes are covered by focused unit tests,
deterministic RAG evals, and bounded local DB materialization evidence.

Recommended next engineering steps:

1. Add embedding task batch caps by total chars/tokens and test queue selection
   against the previously measured long-batch failures.
2. Add admin/debug visibility for `index_policy` and `index_policy_reason`.
3. Build structured block segmentation incrementally for FAQ, instruction,
   list, definition, and table-row chunks.
4. Run opt-in live-answer evals for the negative metadata-only and large PDF
   source-discovery cases.
5. After code review, prepare a separately approved server rollout plan:
   deploy code, run a small server-side sample materialization, inspect output,
   then schedule controlled reindexing.
