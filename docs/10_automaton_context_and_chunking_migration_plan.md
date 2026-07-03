# Перенос контекстного пайплайна, структуры чанков и retrieval-контракта из automaton в vchat

Notion link: n/a

## Назначение документа

Этот документ описывает, что именно нужно перенести в `vchat` из `automaton` на уровне:

- схемы `Chunk` и retrieval-контракта;
- runtime-сборки контекста в `vchat/views/chat/ctx.py`;
- structured citations и structured output для виджета;
- админской наблюдаемости: структура документа, структура чанков, логи чатов, fingerprint пользователя;
- токен-бюджета и интеграции с основным LLM-провайдером `GigaChat`.

Важно:

- extraction / normalization pipeline подробно вынесен в [docs/11_extract_pipeline.md](/Users/xen/Dev/sber/vchat/docs/11_extract_pipeline.md);
- этот документ не про аккуратную миграцию старых данных;
- старые данные можно удалять полностью, если это ускоряет получение правильного итогового поведения.

## Зафиксированные решения

По результатам комментариев к задаче фиксируются следующие решения:

1. В `vchat` нет и не планируется `user memory retrieval`.
   В retrieval остаются только:
   - tail последних сообщений текущего чата;
   - vector retrieval по knowledge base;
   - FTS retrieval по knowledge base;
   - rerank поверх кандидатов.

2. `project_id` в `vchat` не нужен и не переносится.

3. Основной generation provider в `vchat` это `GigaChat API`.

4. Текущий embedder и текущие размеры чанков в `vchat` сохраняются:
   - `embedding_model_id = ai-sage/Giga-Embeddings-instruct`
   - `vec_dim = 2048`
   - `embedding_chunk_max_tokens = 3500`
   - `embedding_chunk_overlap_tokens = 400`
   - `embedding_chunk_max_chars = 12000`

5. Все, что можно надежно считать и поддерживать на стороне БД, должно жить в триггерах, а не в Python.
   Это обязательно для `fts`.

6. Для rollout допустим destructive path:
   - очистка `chunk` обязательна;
   - удаление старых `document` / `chat_msg` / `chat` данных допустимо, если так проще и надежнее;
   - совместимость со старым форматом данных не является целью.

7. Структурированный вывод нужен везде:
   - в developer context payload;
   - в payload цитат для виджета;
   - в админке для просмотра структуры документа и чанков.

## Что в текущем `vchat` не совпадает с целевым состоянием

1. `vchat/views/chat/ctx.py` сейчас собирает только:
   - tail последних сообщений;
   - vector retrieval по `chunk.embedding`;
   - full-text retrieval по `chunk.tsv`;
   - плоский `[context]` message со строковыми snippet bullets и `[[citation:N]]`.

2. В текущем `ctx.py` нет:
   - typed `ContextPayload` / `ContextSnippet` / `SourcePayload` / `ContextPolicy`;
   - structured `[context]` payload;
   - structured `[policy]` payload;
   - reciprocal rank fusion;
   - cross-encoder rerank;
   - query profile c `quote_mode`, `table_mode`, `enumeration_mode`;
   - token-budget trimming через provider abstraction.

3. Текущая модель `Chunk` минимальная:
   - `content`, `tsv`, `embedding`, offsets;
   - без `kind`;
   - без `header_text`;
   - без `section_path`;
   - без `entity_terms`;
   - без `token_count`.

4. Документный chunking сейчас плоский:
   - `_materialize_document_chunks()` использует `chunk_text_word_window(doc.content)`;
   - не сохраняется структура заголовков;
   - не выделяются таблицы;
   - не выделяются `summary` / `section_summary` / `entity_projection`.

5. Есть architectural drift:
   - основной индексатор в `jobs/embedder/tasks.py`;
   - отдельное прямое создание чанков в `jobs/crawler/files_crawler.py`.

6. Full-text retrieval в текущем проекте не опирается на гарантированно поддерживаемый DB-trigger для `chunk.tsv`.

7. В админке нет страницы, где можно посмотреть:
   - структуру документа;
   - нормализованные блоки;
   - materialized snippets;
   - чанки и их типы.

8. В chat widget нет structured citation payload с display path вида:
   - `Благотворительная акция «Мир открытых возможностей» / Итоги акции 2024 года`

9. Пользователи анонимные, но сейчас нет целевого контракта для сохранения:
   - IP;
   - browser / user-agent;
   - device type;
   - JS fingerprint;
   - связанных чатов того же анонимного пользователя в админке.

## Граница ответственности между docs/10 и docs/11

`docs/10` покрывает:

- `Chunk` schema;
- retrieval;
- context packing;
- citations;
- rerank;
- provider/token budgeting;
- chat runtime;
- widget payload;
- admin observability.

`docs/11_extract_pipeline.md` покрывает:

- извлечение сырого содержимого из HTML / PDF / DOCX / TXT / RTF;
- очистку boilerplate;
- нормализацию в markdown-like canonical form;
- построение структурированного представления документа до chunking;
- сохранение структуры для админки и для дальнейшего chunk materialization.

## Целевой контракт данных

### Канонические данные

Каноническими данными остаются:

- `Document.content` как нормализованный markdown-like source of truth;
- `Document.title`, `Document.meta`, `Document.uri`;
- `ChatMsg.text`;
- `Chat.meta` для session/browser/device/fingerprint-метаданных.

### Производные данные

Производными считаются:

- все строки `Chunk`;
- `Chunk.embedding`;
- `Chunk.fts`;
- structured snippets, полученные из документа;
- `ChatMsg.used_chunks`;
- coverage / policy payloads.

Следствие:

- derived-индекс можно пересобирать целиком;
- сохранение старого формата derived-данных не нужно;
- destructive rebuild является нормальным сценарием релиза.

## Предлагаемая форма `Chunk`

`Chunk` нужно привести к retrieval-контракту, близкому к `automaton`, но без полей, которые здесь не нужны.

### Обязательные поля

- `kind`
  - `text`
  - `table`
  - `table_rows`
  - `summary`
  - `section_summary`
  - `entity_projection`
- `header_text`
- `section_path`
- `entity_terms` как `ARRAY(String)`
- `token_count`

### Рекомендуемое выравнивание имен

- `Chunk.content -> Chunk.text`
- `Chunk.tsv -> Chunk.fts`

Это стоит сделать сразу, потому что:

- таблица `chunk` производная;
- старые данные можно удалить;
- дальнейший перенос логики из `automaton` станет проще;
- код retrieval и админки будет чище.

### Поля, которые сознательно не переносим

- `project_id`
- `user memory`-специфичные поля
- `index_version`

`index_version` сознательно убирается из плана.
Для этой задачи нет требования поддерживать несколько поколений индекса одновременно.
Так как старые данные можно удалить, проще делать полный rebuild, чем вводить дополнительную версионность схемы.

## FTS: обязательно через DB trigger

### Обязательные требования

Новая миграция должна:

1. Добавить новые поля `Chunk`.
2. Переименовать `content -> text` и `tsv -> fts`.
3. Создать индексы:
   - GIN index на `chunk.fts`;
   - B-tree index на `chunk.kind`;
   - composite index на `(document_id, kind)`.
4. Создать обязательный DB trigger для пересчета `chunk.fts` при `insert/update`.

### Взвешивание релевантности

Нужно явно усилить FTS по важным частям документа.

Рекомендуемый подход:

- собирать `tsvector` в триггере с весами PostgreSQL:
  - `A` для `Document.title`;
  - `A` или `B` для `Chunk.header_text`;
  - `B` для элементов `section_path`;
  - `C` / `D` для основного `Chunk.text`.

Это не отдельный внешний BM25F-движок, но это правильный PostgreSQL-эквивалент field-weighted FTS для нашей задачи.
То есть title и заголовки действительно получают более высокий коэффициент релевантности.

Нужно прямо зафиксировать это в миграции и в retrieval SQL.

## Структурированные citations

Цель цитирования в `vchat` не просто показать URL источника, а показать, из какой части документа взята цитата.

### Что должно храниться на chunk-level

- `title`
- `header_text`
- `section_path`
- `kind`
- `chunk_ix`
- `score`
- `rerank_score`

### Что должно возвращаться в citation payload

Минимум:

- `citation_id`
- `document_id`
- `chunk_ix`
- `uri`
- `title`
- `kind`
- `header_text`
- `section_path`
- `score`
- `rerank_score`
- `display_path`

`display_path` нужен для пользовательского отображения, например:

- `Благотворительная акция «Мир открытых возможностей» / Итоги акции 2024 года`

Правило построения:

- если `section_path` непустой, она должна участвовать в display label;
- если `header_text` дублирует конец `section_path`, показывать только один раз;
- user-facing widget показывает structured citation label;
- админка показывает полные chunk metadata.

## Retrieval scope

Для `vchat` фиксируется следующий scope:

- knowledge base = все `Chunk`, относящиеся к документам;
- tail = только последние сообщения текущего `chat_id`;
- никаких других чатов того же пользователя в retrieval не участвует.

Важно:

- связанные чаты по fingerprint нужны только для админской диагностики;
- это не означает включение чужих чатов в RAG-контекст.

## Перенос retrieval pipeline

`vchat/views/chat/ctx.py` должен быть переписан по образцу `automaton`, но с адаптацией к модели `vchat`.

### Что переносим

1. Typed models:
   - `ContextSnippet`
   - `ContextPayload`
   - `SourcePayload`
   - `ContextPolicy`
   - `Snippet`

2. Query profiling:
   - `lexical_query()`
   - `queryprofile()`
   - режимы `quote_mode`, `table_mode`, `enumeration_mode`

3. Retrieval buckets:
   - tail messages;
   - knowledge base vector retrieval;
   - full-text supply.

4. Ranking:
   - reciprocal rank fusion;
   - cross-encoder rerank.

5. Structured output:
   - structured `[context]` developer message;
   - structured `[policy]` developer message;
   - `reason_code`;
   - coverage/policy payload.

### Что не переносим

- `project_id` filtering;
- `user memory retrieval`.

### Формат результата `get_context()`

`get_context()` должен возвращать не только историю сообщений, но и структурированный runtime payload:

- `history`
- `sources`
- `used_chunks`
- `coverage`
- `policy`
- `reason_code`

Это нужно и для модели, и для фронтенда, и для админской диагностики.

## Rerank: обязателен и ставится через `make setup`

### Зафиксированное решение

В `automaton` `make setup` уже выкачивает embedding и rerank models.
Такой же паттерн нужно сделать обязательным в `vchat`.

### Требования для `vchat`

1. `make setup` обязательно:
   - ставит Python dependencies;
   - прогревает embedding model;
   - прогревает rerank model.

2. Установка rerank model не должна быть скрытым runtime-side effect первого запроса.

3. Если rerank model отсутствует после `make setup`, это считается broken environment.

### Rerank model

Базовый вариант для parity:

- `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`

Эта модель должна:

- скачиваться в `make setup`;
- прогреваться заранее;
- использоваться в `ctx.py` без ленивой первой сетевой загрузки на production-запросе.

## Provider abstraction и токен-бюджет

Требование про token budget остается обязательным.

### Что нужно изменить в `vchat/ai_providers.py`

`ModelInfo` должен знать:

- `context_window`
- `max_tokens`

`BaseAIProvider` должен уметь:

- считать токены через `token_count()`

### Что важно для текущего проекта

- основной акцент на `GigaChat`;
- новый retrieval-контекст должен pack-иться под лимиты реального GigaChat-model id;
- trimming должен происходить осознанно, а не по грубому хардкоду.

### Что не меняем в этой итерации

- embedder;
- размерность вектора;
- размеры чанков.

То есть migration retrieval/chunking идет поверх текущего:

- `ai-sage/Giga-Embeddings-instruct`
- `2048-dim`
- существующих chunk size параметров.

## Анонимный пользователь, fingerprint и логи чатов

Пользователи в `vchat` всегда анонимны, но это не отменяет потребность в session-level наблюдаемости.

### Что нужно собирать

На уровне `Chat.meta` нужно сохранять:

- `ip_address`
- `user_agent`
- `browser`
- `os`
- `device_type`
- `device_fingerprint`
- дополнительные widget/session markers при необходимости

### Источник данных

- `ip_address` и часть `user_agent` извлекаются на backend;
- `device_fingerprint` приходит с фронтенда;
- chat frontend находится в `frontend_chat`;
- fingerprint должен собираться JavaScript-библиотекой на стороне браузера и передаваться на backend при создании / продолжении чата.

### Где это показывается

- в админке и логах чатов это обязательно видно;
- в пользовательском виджете это не показывается.

### Что должна уметь админка

- открыть карточку чата и увидеть fingerprint / browser / IP / device;
- показать другие чаты с тем же fingerprint;
- использовать fingerprint как основной способ группировки анонимного пользователя;
- при необходимости дополнительно фильтровать по IP / user-agent.

## Страница структуры документа в админке

Это обязательная часть задачи.

Для документа в админке должна появиться отдельная страница, где видно:

- нормализованную структуру документа;
- структурные блоки;
- snippets;
- chunks;
- тип каждого chunk;
- `header_text`;
- `section_path`;
- `token_count`;
- пользовательский display label;
- при необходимости FTS-представление и debug-метаданные.

Эта страница нужна не только для отладки, но и как основной способ проверки качества extraction + chunking.

## Перенос в chat runtime и widget

После переписывания retrieval-контракта нужно обновить:

- `vchat/views/chat/views.py`
- frontend payload для chat widget

### Что меняется для runtime

1. `get_context()` получает provider/model.
2. Context packing учитывает token budget.
3. Response payload включает:
   - `sources`
   - `coverage`
   - `reason_code`
   - structured citation metadata

### Что меняется для user widget

Пользователю нужно показывать не просто ссылку на документ, а структурированную ссылку на раздел документа.

То есть citation block в виджете должен уметь показывать:

- `title`
- `display_path`
- при необходимости `kind`

Пример:

- `Благотворительная акция «Мир открытых возможностей» / Итоги акции 2024 года`

Это пользовательский функционал, не только админский.

## Связь с extraction pipeline

Structured chunking невозможен без полного extraction pipeline из [docs/11_extract_pipeline.md](/Users/xen/Dev/sber/vchat/docs/11_extract_pipeline.md).

Следствие:

- перенос нельзя делать как локальный rewrite только `ctx.py`;
- сначала должен появиться канонический extraction pipeline;
- затем на его основе materialize-ятся `summary`, `section_summary`, `table`, `table_rows`, `entity_projection`, `text`.

## План реализации

### 1. Схема БД и destructive migration

1. Сделать новую Alembic migration для `chunk`.
2. В migration:
   - добавить `kind`, `header_text`, `section_path`, `entity_terms`, `token_count`;
   - переименовать `content` в `text`;
   - переименовать `tsv` в `fts`;
   - создать обязательный trigger для `fts`;
   - создать GIN/B-tree индексы;
   - зафиксировать weighted FTS.
3. Разрешить destructive path:
   - полная очистка `chunk`;
   - при необходимости очистка `document`, `chat_msg`, `chat`.

### 2. Extraction pipeline по docs/11

1. Выровнять ingestion HTML и файлов.
2. Убрать прямое создание `Chunk` из crawler/file pipelines.
3. Сначала получать канонический `Document.content` и `Document.meta.structure`.
4. Только после этого запускать indexing.

### 3. Materialization чанков

1. Портировать document chunker из `automaton`.
2. Сохранить текущий embedder и текущие chunk-size параметры.
3. Материализовать:
   - `summary`
   - `section_summary`
   - `text`
   - `table`
   - `table_rows`
   - `entity_projection`

### 4. Retrieval и rerank

1. Портировать query profile.
2. Портировать RRF.
3. Портировать cross-encoder rerank.
4. Оставить retrieval buckets:
   - tail
   - vector
   - FTS
5. Убрать user-memory branch полностью.

### 5. Token budgeting и GigaChat provider metadata

1. Расширить `ModelInfo`.
2. Добавить `token_count()`.
3. Зафиксировать лимиты для GigaChat как primary runtime.
4. Использовать эти лимиты в trimming.

### 6. Widget и citations

1. Отдавать structured source payload.
2. Показывать пользователю `display_path`.
3. Привязать citation click к structured source item.

### 7. Frontend fingerprint и admin chat logs

1. В `frontend_chat` добавить JS fingerprint library.
2. Передавать fingerprint на backend.
3. Сохранять browser/IP/device/fingerprint в `Chat.meta`.
4. Доработать admin chat history:
   - вывод fingerprint;
   - фильтр по fingerprint;
   - просмотр связанных чатов.

### 8. Admin document structure page

1. Добавить отдельную страницу просмотра структуры документа.
2. Показывать на ней:
   - структуру;
   - snippets;
   - chunks;
   - kinds;
   - section paths;
   - debug metadata.

### 9. `make setup`

1. Сделать прогрев embedding model обязательной частью `make setup`.
2. Сделать прогрев rerank model обязательной частью `make setup`.
3. Исключить сценарий, где первая пользовательская генерация скачивает тяжелые модели.

### 10. Верификация

Минимально обязательные проверки:

1. Документ с заголовками дает корректный `section_path`.
2. Таблица дает `table` и `table_rows`.
3. Citation label показывает section-aware path.
4. FTS действительно усиливает title/header matches.
5. Token trimming работает под лимиты GigaChat.
6. В админке видны fingerprint, browser, IP, device.
7. В админке есть страница структуры документа.

## Ответы на вопросы из комментариев

### Что здесь значит `runtime contract`

Под `runtime contract` имеется в виду не схема БД сама по себе, а набор структур и полей, которыми обмениваются:

- retrieval;
- `ctx.py`;
- websocket/chat runtime;
- widget frontend;
- admin UI.

Если эти структуры не согласованы, перенос будет формально завершен, но продуктово не заработает.

### Что было за поле `index_version`

Это служебная версия формата derived-индекса.
В этом плане она больше не нужна и удалена, потому что:

- старые данные можно удалять;
- многоверсионный индекс поддерживать не требуется;
- полный rebuild проще и надежнее.

### Используется ли BM25F

В текущем `vchat` нет отдельной реализации BM25F.
Целевой план для `vchat`:

- использовать weighted PostgreSQL FTS;
- давать больший вес `title`, `header_text`, `section_path`;
- ранжировать через PostgreSQL ranking functions.

Для нашей системы это правильный способ добиться field-aware релевантности без введения отдельного поискового движка.

## Итог

Перенос в `vchat` нужно делать как перенос полного retrieval-контракта:

- extraction pipeline из `docs/11`;
- materialized structured chunks;
- weighted FTS через trigger;
- vector + FTS + tail + rerank;
- structured citations в виджете;
- token budgeting под GigaChat;
- fingerprint/browser/IP/device в админке;
- admin page для просмотра структуры документа и чанков.

Старые данные не являются ограничением.
Приоритет задачи: быстро получить правильную работающую архитектуру, а не сохранять совместимость с предыдущим форматом.
