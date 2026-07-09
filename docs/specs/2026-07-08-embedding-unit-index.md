# Task: Разделить embedding_unit и page_chunk для надежного индекса

## Goal

После задачи индекс документов хранит embedding один раз на канонический текстовый
фрагмент и отдельно хранит привязку этого фрагмента к странице. Обновление
большого числа страниц не должно запускать лишние embedding-вычисления, не
должно зависеть от DB trigger-ов для поддержки duplicate-состояния и должно
сохранять быстрый retrieval для конечного пользователя.

## Context

- `vchat/models/data.py`: текущий `Chunk` смешивает канонический текст,
  embedding, `page_id`, `chunk_ix`, offsets, section/header metadata, FTS и
  duplicate-state.
- `jobs/crawler/tasks.py`: `materialize_page_chunks()` сейчас удаляет все
  chunks страницы и создает новые строки, затем переиспользует embeddings по
  `text_hash` и помечает page-local duplicate chunks.
- `jobs/embedder/tasks.py`: embedder выбирает pending chunks через
  `Chunk.embedding is null` и завершает `Page.status` через группировку по
  `Chunk.page_id`.
- `vchat/views/chat/ctx.py`: vector retrieval и full-text retrieval читают
  `chunk.page_id AS document_id`, join-ят `page`, фильтруют source и ищут
  summary chunks через тот же `page_id`.
- `vchat/llm_cache.py`: cache payload сейчас включает `chunk_id`,
  `document_id/page_id`, `chunk_ix`, `text_hash` и `uri`.
- `vchat/views/chat/sources.py` и `vchat/templates/projects/document_content.html`:
  source enrichment и debug UI ожидают page-specific chunks.

## Current Behavior

- Одна строка `chunk` одновременно является единицей embedding и привязкой к
  конкретной странице.
- Массовое обновление страниц удаляет старые chunks и вставляет новые, даже если
  текстовый фрагмент уже встречался и embedding можно переиспользовать.
- Duplicate-поддержка частично живет в PostgreSQL trigger-е
  `promote_duplicate_chunk_on_delete()`, что уже привело к хрупкости при
  изменении схемы.
- Full-text ranking сейчас использует `ts_rank_cd` с ручными весами по kind; это
  не полноценный BM25F. Если нужен BM25F, его надо явно проектировать как часть
  page-specific retrieval, а не считать существующим поведением.
- Внутренний retrieval использует имя `document_id` для `page_id`, хотя
  стабильным доменным идентификатором должен быть `page_id`.

## Target Shape

- Новая сущность `embedding_unit` хранит канонический текстовый фрагмент:
  нормализованный `text_hash`, normalized text key или checksum, исходный text,
  token_count, embedding, embedding model/version metadata и timestamps.
- Новая сущность `page_chunk` хранит связь со страницей и page-specific контекст:
  `page_id`, `embedding_unit_id`, `chunk_ix`, offsets, kind, header_text,
  section_path, entity_terms, page-specific FTS/ranking payload и служебные поля
  materialization.
- `page_id` остается стабильным внешним и внутренним идентификатором страницы.
  `document_id` должен быть устранен из новых контрактов retrieval/cache; старые
  внутренние упоминания можно удалить в рамках миграции к новой модели.
- `embedding_unit.id` является внутренним идентификатором embedding-единицы и не
  должен становиться пользовательским или cache-stable идентификатором.
- Retrieval должен возвращать page-specific hit: `page_chunk` определяет страницу,
  порядок, section/header context и source filter; `embedding_unit` дает text и
  vector distance.
- Pending embedding считается по `embedding_unit.embedding is null`, а готовность
  страницы считается по `page_chunk` этой страницы и связанным
  `embedding_unit`.
- Page rebuild должен заменять набор `page_chunk` для страницы без удаления
  `embedding_unit`, если тот используется другими страницами.
- Duplicate-поведение должно быть явным Python-кодом или SQL-запросами в
  индексирующем pipeline, без trigger-ов, которые скрыто меняют строки при
  delete/update.
- BM25F надо проверить отдельно: либо внедрить явный page-specific ranking,
  либо зафиксировать текущий PostgreSQL FTS как отдельный, не BM25F, слой.

## Guard Rails

- Не менять crawler lifecycle: route/API должны по-прежнему ставить страницу в
  существующий `crawl_page_task` / `crawl_source_task`, а скачивание,
  extraction, shingles и индексация остаются в `jobs/`.
- Не выполнять embedding в aiohttp request/websocket event loop.
- Не добавлять fallback-логику, compatibility wrappers или старые task names без
  отдельного подтверждения.
- Не считать `embedding_unit.id` стабильным пользовательским идентификатором.
- Не расширять доступ публичных виджетов к данным: source filtering остается
  запрещающим по умолчанию.
- Не читать и не править `docs/` в рамках реализации этой задачи.
- Не смешивать миграцию индекса с изменением качества chunking/extraction, кроме
  минимальных правок, необходимых для новой схемы.
- Полная переиндексация допустима, но только после явного сравнения с переносом
  существующих embeddings; предпочтение за переносом, если он не усложняет задачу
  сильнее, чем безопасный rebuild.

## Iterations

1. **Зафиксировать контракт индекса.**
   - Описать таблицы `embedding_unit` и `page_chunk`, ключи, уникальности,
     индексы, статусные переходы и правила удаления.
   - Контрольная точка: короткий schema contract в PR/diff, где видно, какие
     поля уходят из текущего `chunk`, какие остаются page-specific, и как
     считается готовность страницы.

2. **Добавить новую схему без переключения read-path.**
   - Добавить модели и миграцию для `embedding_unit` и `page_chunk`.
   - Не удалять старый `chunk` на этом шаге.
   - Контрольная точка: миграция поднимается на локальной БД, новые таблицы
     пустые, старые retrieval и embedder продолжают работать.

3. **Переписать materialization write-path.**
   - `materialize_page_chunks()` создает или находит `embedding_unit` по
     нормализованному тексту и пересобирает `page_chunk` для страницы.
   - Убрать необходимость `promote_duplicate_chunk_on_delete()`.
   - Контрольная точка: повторная индексация одной страницы не создает новый
     `embedding_unit`, если текстовые фрагменты не изменились.

4. **Переписать embedder на embedding_unit.**
   - Pending queue выбирает `embedding_unit` без vector.
   - После записи embedding пересчитывается готовность страниц через
     `page_chunk`.
   - Контрольная точка: один embedding-расчет закрывает все страницы, которые
     ссылаются на тот же `embedding_unit`.

5. **Переписать retrieval и cache contracts на page_chunk.**
   - Vector retrieval ищет по `embedding_unit.embedding`, но возвращает
     page-specific `page_chunk`.
   - Full-text retrieval работает по page-specific FTS/ranking payload.
   - `Snippet.document_id` и SQL aliases `document_id` заменить на `page_id` в
     новых контрактах.
   - Контрольная точка: source filters, summaries, context payload, LLM cache и
     source cards работают через `page_id`.

6. **Разобрать FTS/BM25F.**
   - Проверить текущие `ts_rank_cd` запросы против требований к BM25F.
   - Выбрать реализацию: PostgreSQL FTS с явными полями/весами, отдельный BM25F
     расчет, или гибрид с сохранением текущего behavior под честным названием.
   - Контрольная точка: тесты показывают, что заголовок страницы, section/header
     и text участвуют в ранжировании ожидаемым образом.

7. **Мигрировать данные и удалить старую модель.**
   - Выбрать перенос embeddings или полную переиндексацию.
   - Удалить старые DB trigger-и и поля/таблицы, которые больше не нужны.
   - Контрольная точка: после миграции нет production-кода, который читает
     `chunk.page_id`, `chunk.embedding`, `chunk.is_duplicate` или
     `duplicate_of_chunk_id`.

## Verification

- **Критерии успеха**
  - Повторная индексация страницы с теми же фрагментами не увеличивает число
    `embedding_unit` и не ставит новые embedding-задачи.
  - Две страницы с одинаковым нормализованным фрагментом используют один
    `embedding_unit`, но retrieval возвращает разные `page_id`, URI, title,
    source и section context.
  - Массовое обновление source не выполняет delete/update trigger-логику на
    embedding-единицах и не оставляет страницы в `parsing` из-за скрытых DB
    side effects.
  - `pending_embeddings` отражает количество `embedding_unit` без vector, а
    готовность page отражает все page_chunks этой page.
  - Source filter в widget/chat retrieval ограничивает результаты через
    `page.source_id` и не расширяет доступ к чужим страницам.
  - В новых retrieval/cache payloads используется `page_id`, а не `document_id`.

- **Критерии неуспеха**
  - Один `embedding_unit` содержит page-specific FTS/title/section данные.
  - Retrieval hit теряет связь с конкретной page или source.
  - LLM cache становится нестабильным из-за внутреннего `embedding_unit.id`.
  - Массовый rebuild создает embedding-задачи для уже известных текстов.
  - Появляются fallback-и или compatibility shim-и для старой схемы без явного
    решения.
  - Full-text слой называется BM25F без проверенного BM25F-подобного контракта.

- **Проверки по итерациям**
  - Миграционные проверки: upgrade/downgrade на свежей локальной БД; проверка
    индексов для vector KNN, `page_chunk.page_id`, `embedding_unit.text_hash`,
    pending embeddings и source-filter join.
  - Unit-тесты: materialization reuse, page readiness, pending queue selection,
    duplicate text across pages, отсутствие trigger-зависимости.
  - Integration-тесты: crawl/update одной страницы, refresh source slice,
    vector retrieval, fulltext retrieval, source cards, LLM cache payload.
  - Data checks: counts before/after migration по pages, page_chunks,
    embedding_units, pending embeddings; выборка одинаковых `text_hash` должна
    иметь один `embedding_unit` и несколько `page_chunk`.
  - Performance checks: `EXPLAIN` для vector KNN и fulltext retrieval; массовый
    reindex source не должен деградировать из-за join explosion.

## Open Questions

- Нужна ли полноценная реализация BM25F в этой задаче или достаточно
  зафиксировать честный PostgreSQL FTS ranking и вынести BM25F в отдельную
  задачу?
- Надо ли сохранять старые LLM cache entries после переименования
  `document_id` в `page_id`, или cache можно инвалидировать при смене схемы?
- Должен ли `embedding_unit.text` хранить исходный текст первого фрагмента или
  нормализованный канонический текст, если разные page_chunks отличаются только
  невидимыми символами/пробелами?
- Нужен ли отдельный `embedding_model_version` уже в первой миграции, чтобы
  будущая смена embedding model не смешивала vectors разных моделей?
