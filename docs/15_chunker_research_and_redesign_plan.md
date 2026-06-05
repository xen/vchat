# Исследование chunker и план переработки стратегии

Notion link: n/a

Нужно разобраться, почему в `embedder` появляются документы и блоки размером в сотни тысяч и миллионы токенов, и описать новую стратегию chunking, которая не позволит одному pathological document остановить индексацию.

## Current state

Текущий ingestion path для crawl-страниц проходит через `extract_url_document()` в [jobs/crawler/document_pipeline.py](/Users/xen/Dev/sber/vchat/jobs/crawler/document_pipeline.py:555), затем сохраняет результат в `Page.content` и `Page.meta`, после чего ставит `jobs.embedder.tasks.index_document` в очередь из [jobs/crawler/pipelines.py](/Users/xen/Dev/sber/vchat/jobs/crawler/pipelines.py:193) и [jobs/crawler/pipelines.py](/Users/xen/Dev/sber/vchat/jobs/crawler/pipelines.py:298).

Каноническим источником для chunking сейчас является `Page.content` из [vchat/models/data.py](/Users/xen/Dev/sber/vchat/vchat/models/data.py:128). Производные артефакты:

- `Page.meta["structure"]` и `Page.meta["outline"]`, собранные в [jobs/crawler/document_pipeline.py](/Users/xen/Dev/sber/vchat/jobs/crawler/document_pipeline.py:378)
- `Chunk` rows, создаваемые из `Page.content` в [jobs/embedder/tasks.py](/Users/xen/Dev/sber/vchat/jobs/embedder/tasks.py:674)
- `embedding` в `Chunk`, который считается позже в `pending_chunks()` из [jobs/embedder/tasks.py](/Users/xen/Dev/sber/vchat/jobs/embedder/tasks.py:1100)

Текущий runtime path разбит на две стадии:

1. `index_document()` materialize-ит все `Chunk` с `embedding = NULL` из [jobs/embedder/tasks.py](/Users/xen/Dev/sber/vchat/jobs/embedder/tasks.py:1085).
2. `pending_chunks()` дозаполняет эмбеддинги по одному чанку из [jobs/embedder/tasks.py](/Users/xen/Dev/sber/vchat/jobs/embedder/tasks.py:731).

Это значит, что если stage 1 долго крутится на одном документе, очередь embeddings перестает продвигаться вообще, даже если проблема еще не дошла до `model.encode()`.

Текущая структура extraction и chunking уже допускает oversized documents в нескольких местах:

- `_html_to_markdown_like()` собирает markdown-like text из всех `h1..h6`, `p`, `li`, `pre`, `code`, `table` без лимита на общий размер и без page-level guard в [jobs/crawler/document_pipeline.py](/Users/xen/Dev/sber/vchat/jobs/crawler/document_pipeline.py:446).
- `extract_url_document()` принимает этот результат как successful extraction, если `markdown.strip()` не пустой, и не делает quality/size gate перед сохранением в `Page.content` в [jobs/crawler/document_pipeline.py](/Users/xen/Dev/sber/vchat/jobs/crawler/document_pipeline.py:577).
- `build_document_payload()` дублирует извлеченный текст в `meta["structure"]`, потому что структура хранит полное `content` каждого paragraph/table/code block в [jobs/crawler/document_pipeline.py](/Users/xen/Dev/sber/vchat/jobs/crawler/document_pipeline.py:389).
- crawler сохраняет и `page.content`, и полный `meta`, что удваивает объем данных на каждую проблемную страницу в [jobs/crawler/pipelines.py](/Users/xen/Dev/sber/vchat/jobs/crawler/pipelines.py:285) и [jobs/crawler/pipelines.py](/Users/xen/Dev/sber/vchat/jobs/crawler/pipelines.py:298).

Текущий chunker строит blocks только по двум типам границ:

- markdown headings
- markdown tables

Это видно в [jobs/embedder/tasks.py](/Users/xen/Dev/sber/vchat/jobs/embedder/tasks.py:386) и [jobs/embedder/tasks.py](/Users/xen/Dev/sber/vchat/jobs/embedder/tasks.py:410).

Если extraction выдает большой документ без заголовков или без корректных line breaks, весь хвост попадает в один giant `text` block. Внутри такого блока возникают следующие проблемы:

- `collect_entity_terms()` гоняет regex по всему блоку целиком в [jobs/embedder/tasks.py](/Users/xen/Dev/sber/vchat/jobs/embedder/tasks.py:331).
- `block_tokens = tokenizer(block, truncation=False)` токенизирует весь giant block целиком еще до разбиения на окна в [jobs/embedder/tasks.py](/Users/xen/Dev/sber/vchat/jobs/embedder/tasks.py:552).
- `chunk_text_word_window()` на каждом шаге собирает `candidate_text = " ".join([*chunk_tokens, token])` и повторно токенизирует его без incremental accounting, то есть работает квадратично на длинных блоках в [jobs/embedder/tasks.py](/Users/xen/Dev/sber/vchat/jobs/embedder/tasks.py:212).

Для таблиц есть отдельный, но похожий риск:

- `split_table_rows()` повторно токенизирует весь `"\n".join(bucket)` на каждой строке, что тоже квадратично при длинных table-like blocks в [jobs/embedder/tasks.py](/Users/xen/Dev/sber/vchat/jobs/embedder/tasks.py:284).

Есть еще один отдельный ingestion path для ручных файлов. Редактор в [vchat/views/projects/views.py](/Users/xen/Dev/sber/vchat/vchat/views/projects/views.py:1865) сохраняет raw user content прямо в `Page.content`, удаляет старые `Chunk` и сразу вызывает `schedule_index_document(document.id)` из [vchat/views/projects/views.py](/Users/xen/Dev/sber/vchat/vchat/views/projects/views.py:1916). Этот путь обходит extraction и structured normalization из `jobs/crawler/document_pipeline.py`, поэтому большой pasted document или raw markdown file тоже может превратиться в giant block.

Текущая защита от oversized content в рабочем дереве только временная:

- `materialize_page_chunks()` режет document по `EMBEDDING_DOCUMENT_MAX_CHARS` перед chunking в [jobs/embedder/tasks.py](/Users/xen/Dev/sber/vchat/jobs/embedder/tasks.py:681).

Эта защита снимает аварийный стопор, но не решает архитектурную проблему. Она обрезает симптом, а не меняет стратегию segmentation.

Релевантные файлы:

- `jobs/crawler/document_pipeline.py`
- `jobs/crawler/pipelines.py`
- `jobs/embedder/tasks.py`
- `vchat/models/data.py`
- `vchat/views/projects/views.py`
- `vchat/views/api/views.py`
- `vchat/document_shingles.py`
- `tests/test_document_pipeline.py`
- `tests/test_document_shingles.py`
- `tests/test_embedder_chunking_limits.py`

## Models and data

Канонические сущности сейчас:

- `Page.content` в [vchat/models/data.py](/Users/xen/Dev/sber/vchat/vchat/models/data.py:139)
- `Page.meta` в [vchat/models/data.py](/Users/xen/Dev/sber/vchat/vchat/models/data.py:147)

Производные сущности:

- `Chunk.text`, `Chunk.kind`, `Chunk.token_count`, `Chunk.embedding` в [vchat/models/data.py](/Users/xen/Dev/sber/vchat/vchat/models/data.py:253)
- `SourceShingleFreq` для boilerplate filtering в [vchat/models/data.py](/Users/xen/Dev/sber/vchat/vchat/models/data.py:375)

Главная проблема data shape сейчас в том, что `Page.meta["structure"]` хранит полный текст блоков, а не compact structural metadata. Для больших страниц это создает две копии почти одного и того же payload:

- полная строка в `Page.content`
- почти тот же текст по блокам в `Page.meta["structure"]`

Для redesign стоит разделить:

- canonical document text
- compact structure for navigation and debugging
- derived chunk materialization

Рекомендуемое направление:

1. Оставить `Page.content` каноническим normalized text.
2. Сжать `Page.meta["structure"]` до bounded form:
   - `type`
   - `level`
   - `section_path`
   - `start_char`
   - `end_char`
   - optional short preview
3. Не хранить полный paragraph/table/code payload внутри `Page.meta["structure"]`.
4. Если нужен полный debug artifact extraction stage, хранить его отдельно от hot-path `Page` row.

Дополнительно нужно ввести явные bounded limits в config для каждого этапа:

- `extraction_document_max_chars`
- `extraction_structure_max_blocks`
- `chunk_block_max_chars`
- `chunk_table_max_rows`
- `chunk_table_preview_max_rows`
- `chunk_entity_scan_max_chars`

## Implementation plan

1. Schema and model changes

- Пересмотреть контракт `Page.meta["structure"]` из [jobs/crawler/document_pipeline.py](/Users/xen/Dev/sber/vchat/jobs/crawler/document_pipeline.py:392), чтобы он больше не дублировал весь текст.
- Явно разделить компактную `outline`-информацию для UI и heavy debug-данные extraction.
- Если heavy debug-данные все еще нужны, вынести их из hot row `page` в отдельную таблицу или артефактный storage.

2. Background jobs and lifecycle rules

- Сохранить двухступенчатую схему `index_document -> pending_chunks`, но гарантировать, что `index_document` работает только на bounded intermediate blocks.
- Не позволять `index_document()` materialize-ить giant blocks из raw text без предварительного segmentation by block type.
- Для oversized documents делать явное state transition:
  - либо `status_error = oversized_content`
  - либо partial indexing с пометкой truncation в `meta["extraction"]`
- Для manual file editing path в [vchat/views/projects/views.py](/Users/xen/Dev/sber/vchat/vchat/views/projects/views.py:1889) прогонять документ через тот же normalization pipeline, а не отправлять raw text сразу в chunker.

3. Read/write paths and backend handlers

- В `extract_url_document()` добавить quality gate перед возвратом success-path:
  - total chars
  - longest line
  - longest block preview
  - block count
  - table row count
- В `_html_to_markdown_like()` добавить bounded extraction:
  - max total chars
  - max text per element
  - skip pathological nodes with enormous text payload
- Для `doc_type`-aware handling использовать уже существующий `guess_document_type()` из [vchat/document_types.py](/Users/xen/Dev/sber/vchat/vchat/document_types.py:160) не только как label, а как switch для стратегии сегментации.

4. Chunking strategy redesign

- Перейти от current "split by headings/tables, then sliding word window" к typed segmentation:
  - paragraph blocks
  - heading blocks
  - list blocks
  - code blocks
  - table blocks
  - fallback plain-text blocks
- Внутри `chunk_document_text()` сначала строить bounded semantic blocks, потом уже разбивать каждый block локально.
- Для giant text blocks применять многоступенчатое деление:
  - split by headings
  - then blank lines / paragraph boundaries
  - then sentence-ish boundaries
  - only потом token windows
- Для table-like content:
  - ограничить число rows per chunk
  - ограничить total table rows per page in embedding path
  - CSV/code-like documents не гнать через HTML/table projection path без отдельной normalization strategy
- `chunk_text_word_window()` переписать на incremental token accounting:
  - не собирать `candidate_text` и не токенизировать весь prefix на каждом шаге
  - токенизировать отдельные units заранее
  - вести running token length
- `collect_entity_terms()` ограничить по размеру входа или перенести на уже bounded preview блока.

5. Generated artifacts and rebuild triggers

- После изменения chunk contract потребуется refresh/reindex existing `Page` rows, потому что:
  - изменится segmentation
  - изменится shape `meta["structure"]`
  - giant pages могут перейти из "индексируем как есть" в "truncate/skip/special-case"
- `refresh_project_index()` из [jobs/embedder/tasks.py](/Users/xen/Dev/sber/vchat/jobs/embedder/tasks.py:1167) и `refresh_source_index()` из [jobs/embedder/tasks.py](/Users/xen/Dev/sber/vchat/jobs/embedder/tasks.py:1246) должны учитывать новую политику oversized documents и новую shape extraction metadata.

6. Tests and verification

- Добавить tests на giant unheaded paragraphs.
- Добавить tests на very long single lines.
- Добавить tests на huge HTML tables и CSV-like payload.
- Добавить tests на manual file path, который должен сначала нормализовать content.
- Добавить perf-oriented regression tests хотя бы на bounded behavior:
  - chunker не должен создавать giant intermediate strings
  - block tokenizer не должен вызываться квадратично для типичного long block
  - extraction не должна писать full duplicated content в `Page.meta["structure"]`

## Summary and recommendation

Проблема не сводится к одному багу в overlap logic. Баг был реальный и уже подтвердил, что `chunk_text_word_window()` мог не двигаться по входу. Но даже после этого текущая архитектура все еще допускает pathological documents и pathological blocks:

- extraction не ограничивает размер документа;
- structure metadata дублирует контент;
- segmentation слишком грубая;
- token accounting внутри chunker квадратичное;
- manual files обходят extraction pipeline.

Рекомендуемая стратегия:

- считать chunking не "функцией, которая режет строку", а второй фазой после bounded structural segmentation;
- сделать doc-type-aware segmentation;
- убрать дублирование текста в `Page.meta`;
- ввести явные limits и policies для oversized documents;
- выровнять manual files и crawler pages в один normalization contract.

## Risks

- Жесткая truncation без typed policy может silently терять важный контент.
- Полный skip oversized documents без UI/diagnostics затруднит операционную поддержку.
- Если оставить `meta["structure"]` как сейчас, даже исправленный chunker не уберет лишнюю нагрузку на БД и сериализацию.
- Если ограничиться только `EMBEDDING_DOCUMENT_MAX_CHARS`, то giant documents перестанут падать, но будут индексироваться непредсказуемо и частично.

## Open questions

- Нужен ли для giant code/csv-like documents отдельный ingestion policy вместо общего markdown/html path?
- Должны ли giant hub pages индексироваться частично, или их лучше понижать в приоритете и skip-ать из embeddings совсем?
- Нужен ли отдельный persisted debug artifact extraction stage, или достаточно компактного `outline` и bounded counters в `Page.meta`?
- Нужно ли вводить новый `PageStatusError` для oversized/truncated documents, чтобы это было видно в админке и reindex flow?

## Follow-up work

- Подготовить конкретный patch plan для `jobs/crawler/document_pipeline.py` и `jobs/embedder/tasks.py`.
- Принять решение по новому контракту `Page.meta["structure"]`.
- После этого делать controlled reindex для уже сохраненных больших страниц.
