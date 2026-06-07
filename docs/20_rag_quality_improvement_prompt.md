# Prompt: RAG Quality Improvement Loop

Ты работаешь в репозитории `/Users/xen/Dev/sber/vchat`.

Нужно запустить длительный инженерный цикл улучшения качества ответов по контенту проиндексированных сайтов. Работай автономно, но сохраняй высокую инженерную планку: проверяй факты по данным, пиши тесты, регулярно делай саморевью, фиксируй выводы в `docs/`, не добавляй legacy/fallback-слои без явного согласования.

## Context

Продукт индексирует сайты и документы клиентов, затем отвечает пользователю по проиндексированному контенту. Сейчас есть несколько связанных проблем:

- chunking местами слишком наивный и может создавать много мусорных chunks;
- retrieval приносит лишние источники и нерелевантный контекст;
- этап формирования ответа плохо протестирован и может использовать мусорный контекст;
- в базе уже есть реальные сайты клиентов, поэтому можно измерить фактический импакт, а не спорить абстрактно;
- некоторые страницы и файлы должны быть discoverable, но не должны индексироваться как полноценный текстовый контент.

Конечная цель: пользователь получает качественные, grounded ответы по контенту проиндексированных сайтов, с корректными ссылками на источники и без лишнего мусора.

## Non-Negotiable Engineering Rules

- Следуй `AGENTS.md`.
- Используй только `venv/bin/python`, `venv/bin/pytest` и другие entrypoints из project venv.
- Fail fast. Не добавляй silent fallback, tolerant parsing, backwards compatibility wrappers или deprecated paths без явного разрешения.
- Не скрывай инфраструктурные и data-проблемы `try/except`, если исключение не re-raised.
- Не удаляй источники (`source`) при экспериментах с данными.
- Все работы пока проводи локально. Не трогай тестовый/production сервер, пока локально не будет доказано улучшение качества.
- Текущая база embedding vectors не является ценным артефактом для этого этапа. Можно локально пересоздавать chunks/embeddings для небольших срезов контента, чтобы измерять качество и сравнивать варианты pipeline.
- Не ломай текущий production/test-server pipeline. Серверные изменения и переиндексация на сервере только после отдельного явного разрешения.
- Все существенные решения и результаты фиксируй в `docs/`.
- Перед финальным ответом сделай self-review: что изменено, какие тесты запущены, какие риски остались.

## Goal

Добиться измеримого улучшения качества RAG pipeline:

1. Лучше отбирать индексируемый контент.
2. Лучше chunking под реальные пользовательские интенты.
3. Лучше retrieval/rerank/context assembly.
4. Лучше финальное формирование ответа.
5. Создать тестовую базу и eval loop, чтобы качество можно было регрессионно проверять.

Важно: не начинать с большого redesign вслепую. Сначала измерить фактический импакт потенциальных улучшений на реальной базе.

## Required Work Mode

Запусти Ralph loop / автономный цикл достижения цели:

1. Measure current baseline.
2. Identify the biggest quality and cost problems from real data.
3. Propose one small, high-impact intervention.
4. Implement it with tests.
5. Run evals and compare against baseline.
6. Review code and results.
7. Decide whether to keep, adjust, or revert.
8. Repeat until there is a defensible quality improvement.

Do not jump directly to a broad rewrite.

Важно: при изменении pipeline chunking/retrieval можно и нужно запускать локальные end-to-end прогоны на ограниченных наборах данных:

- выбрать небольшой набор страниц/документов из реального корпуса;
- пересоздать chunks для этого набора;
- пересчитать embeddings локально;
- прогнать retrieval/context/answer eval от начала до конца;
- сравнить с baseline;
- сохранить результаты в `docs/`.

Полные локальные прогоны от extraction/chunking до финального ответа разрешены, если они bounded и нужны для проверки качества. Сервер при этом не трогать.

## Phase 1: Baseline and Data Audit

First, inspect the current code paths:

- `jobs/embedder/chunking.py`
- `jobs/embedder/tasks.py`
- `jobs/embedder/queue.py`
- `jobs/crawler/document_pipeline.py`
- `jobs/crawler/pipelines.py`
- `vchat/views/chat/ctx.py`
- `vchat/models/data.py`
- existing docs:
  - `docs/15_chunker_research_and_redesign_plan.md`
  - `docs/17_embedding_optimization_report.md`
  - `docs/18_reindex_monitoring_report.md`
  - `docs/19_chunking_strategy_recommendations.md`

Then measure the real indexed corpus, preferably on the test server if available:

- pages by source/status/status_error/content_value/content length;
- chunks by source/kind/token_count/text length;
- chunk count per page p50/p90/p99/max;
- token count per chunk p50/p90/p99/max;
- biggest pages by chunk count;
- biggest chunks by char/token count;
- sources/pages with high boilerplate or low content value;
- ratio of `summary`, `section_summary`, `entity_projection`, `text`, `table`, `table_rows`;
- retrieval results for representative real queries.

Create a document in `docs/` with baseline numbers and initial hypotheses. Do not implement changes until this baseline exists.

## Phase 2: Impact Estimation Before Redesign

Before changing chunking strategy broadly, estimate impact from data:

- If we add embedding batch char/token caps, estimate p90/p99 task duration improvement from current chunk char distribution.
- If we filter giant CSV/statistical pages, estimate reduced chunks, embeddings, storage, and queue time.
- If we suppress `entity_projection` or summaries from final answer context, estimate how often they currently enter context.
- If we introduce typed chunk policies, identify real pages where current chunks are bad and expected better chunk types.
- If we skip/metadata-only index downloadable files, estimate how many pages/files it affects.
- For promising chunking changes, run local partial re-embedding on small content slices and compare retrieval/answer quality against baseline.

Produce a ranked table:

- idea;
- expected user-quality impact;
- expected indexing/runtime impact;
- implementation risk;
- required tests/evals;
- recommendation: do now / later / reject.

## Phase 3: Build a RAG Quality Test Base

Create an eval dataset from real indexed content. The goal is not a perfect benchmark, but a practical regression suite.

The eval base must include:

- exact fact lookup;
- FAQ/help answer;
- procedural/instruction answer;
- table/numeric lookup;
- quote/source request;
- broad page/source summary;
- multi-section enumeration;
- negative query where the answer is absent;
- noisy source where retrieved context includes irrelevant chunks;
- downloadable document query.

For each test case store:

- user query;
- expected source URL or source title;
- expected answer facts;
- forbidden claims or forbidden source types;
- whether exact citation is required;
- acceptable answer notes;
- current baseline result.

Prefer storing this in a repo-native format under `tests/fixtures/` or `docs/evals/`, plus an executable eval runner under `tests/rag_quality/` or similar.

The eval runner should test at least:

- retrieved chunk relevance;
- source precision;
- context noise;
- answer groundedness;
- whether the answer cites the right URL/page;
- whether the answer refuses or says "not found" when content is absent.

Do not rely only on subjective manual inspection. Add deterministic checks where possible.

## Phase 4: Content Classification and Indexing Policy

Design and implement metadata-only or reduced-index policies for content that should be discoverable but not fully embedded.

Required cases:

### Giant CSV / Statistical Data

Example: one project indexed Dota 2 game statistics from a huge CSV-like file. This is mostly noise for natural-language answers.

Policy needed:

- detect CSV/statistical dumps by content type, extension, table width/row count, numeric density, delimiter patterns, and low natural-language ratio;
- do not full-text chunk/embed the entire content;
- still keep a page/file record so the assistant can answer that the file exists and provide a link;
- generate a small `file_summary` or metadata chunk: filename/title, source URL, file type, size if known, short description, maybe first header row;
- mark indexing policy in metadata, e.g. `index_policy=metadata_only` or equivalent;
- make this visible in admin/debug stats.

### Word / PDF / PowerPoint Documents

Required behavior:

- The system must know the document exists.
- It must be able to answer what the document appears to be about.
- It must provide the link/page where it can be downloaded.
- It does not always need full content QA over the document body.

Policy needed:

- classify documents by type;
- extract bounded metadata and short summary/outline when possible;
- optionally support full content indexing only when file size/content quality is within policy;
- avoid indexing huge or low-value document bodies blindly;
- preserve source URL/download URL.

## Phase 5: Retrieval and Answer Quality

Audit `vchat/views/chat/ctx.py` and related answer generation code.

Look specifically for:

- why irrelevant sources enter context;
- whether summary/projection chunks crowd out direct evidence chunks;
- whether source diversity rules are too weak or too strong;
- whether table mode, quote mode, and enumeration mode behave correctly;
- whether context assembly should exclude some chunk kinds by default;
- whether answer generation has enough policy to say "not found in indexed sources";
- whether citations/source payloads are aligned with actual used chunks.

Implement improvements only with eval coverage.

Likely first interventions:

- exclude `entity_projection` from final answer context by default;
- demote `summary`/`section_summary` in final context unless they route to supporting text chunks;
- add stricter source precision for quote/exact-fact requests;
- add negative-answer behavior when retrieval confidence is too low;
- add source/section diversity for enumeration queries.

## Phase 6: Chunking Improvements

Use `docs/19_chunking_strategy_recommendations.md` as design input, but implement incrementally.

Preferred sequence:

1. Add embedding task char/token caps.
2. Add metadata-only policy for giant CSV/statistical documents.
3. Add file/document metadata chunks for downloadable files.
4. Add or refine chunk kinds only where evals prove current behavior is bad.
5. Add structured block segmentation for high-value cases: FAQ, instructions, lists, definitions, tables.

Avoid broad chunker rewrites without a baseline eval comparison.

For every non-trivial chunking change:

- run a local partial reindex/re-embedding experiment on representative pages;
- include at least one noisy page, one normal content page, one table-like page, and one downloadable/document-like page if available;
- run retrieval and answer evals before/after;
- document whether the change improved user-visible answer quality, not only chunk counts or runtime.

## Phase 7: Review Loop

After each implementation batch:

- run focused unit tests;
- run RAG quality evals;
- compare metrics before/after;
- inspect several failed cases manually;
- write a short review note in `docs/`;
- check for technical debt:
  - no duplicated logic;
  - no broad fallbacks;
  - no hidden retries;
  - no unbounded parsing;
  - no unbounded tokenization;
  - no schema changes without migration/tests;
  - no silent content loss without metadata/status.

If an intervention improves runtime but harms answer quality, do not keep it without an explicit product tradeoff note.

## Deliverables

Required deliverables:

1. Baseline data audit report in `docs/`.
2. Ranked impact estimate table.
3. RAG quality eval dataset.
4. Executable eval runner.
5. One or more focused implementation changes with tests.
6. Before/after quality and runtime comparison.
7. Final recommendation document:
   - what changed;
   - what improved;
   - what did not improve;
   - remaining risks;
   - next engineering steps.

## Definition of Done

The task is not done when code merely compiles.

It is done when:

- baseline metrics exist;
- eval cases exist and run;
- at least one meaningful quality or noise-reduction improvement is implemented;
- before/after comparison is documented;
- tests pass;
- code has been self-reviewed;
- remaining risks are explicit;
- recommendations are grounded in observed data from the real corpus.

## Suggested First Step

Start by creating a short plan and then immediately measure the current corpus:

- DB counts and distributions;
- worst pages/chunks;
- chunk kind distribution;
- sample retrieval outputs for 10-20 real queries;
- examples of bad context/noisy sources.

Only after that choose the first intervention.

Remember: local quality first, server later. Do not deploy, restart server services, clear server Redis, or reindex server data during this task unless explicitly instructed in a later message.
