# Task: Разделение чанков базы знаний и векторов сообщений чата

## Goal

Развести данные базы знаний и данные чатов на уровне схемы и кода:

- страницы и RAG-чанки базы знаний хранятся отдельно от сообщений чата;
- сообщения остаются основным источником истории в `chat_msg`;
- вектора сообщений сохраняются в `chat_msg` для будущих проектов;
- текущий RAG по страницам сохраняет поведение: поиск по базе знаний,
  full-text, rerank, источники и `used_chunks` продолжают работать.

## Context

- `vchat/models/data.py`
- `jobs/embedder/tasks.py`
- `jobs/embedder/queue.py`
- `jobs/crawler/tasks.py`
- `jobs/indexing/documents.py`
- `vchat/views/chat/ctx.py`
- `vchat/views/chat/views.py`
- `vchat/views/chat/sources.py`
- `vchat/views/projects/views.py`
- `vchat/views/api/views.py`

## Current Behavior

- `chat_msg` хранит историю чатов: текст, роль, `full_context`, `used_chunks`,
  provider/model/tokens и guardrail-поля.
- `chunk` сейчас смешивает два смысла:
  - чанки страниц базы знаний через `page_id`;
  - производные чанки сообщений через `chat_id` и `msg_id`.
- После сохранения user/assistant сообщений код ставит
  `jobs.embedder.tasks.index_chat_message`, которая режет текст сообщения на
  `Chunk` и сохраняет `embedding`.
- Retrieval использует две ветки:
  - KB vector search по `chunk.page_id`;
  - chat vector search по `chunk.chat_id`.
- Обычная короткая история чата уже берется из `chat_msg` через `tail_messages`.

## Target Shape

1. Таблица `chunk` остается таблицей чанков страниц и базы знаний.
2. Убрать из `chunk` поля, относящиеся к сообщениям:
   `chat_id`, `msg_id`.
3. Вектора сообщений сохраняются в конкретном поле таблицы `chat_msg`.
4. Chat-vector ветка retrieval остается, но читает embeddings из `chat_msg`, а
   не из `chunk`.

## Proposed Schema

Консервативный вариант:

- `chunk`
  - только для страниц и базы знаний;
  - содержит `page_id`, `chunk_ix`, offsets, kind/header/section/entities,
    `text`, `text_hash`, duplicate fields, `fts`, `embedding`;
  - сохраняет HNSW KB index и FTS index.
- `chat_msg`
  - существующие поля сообщения;
  - `embedding vector(1024)`;
  - `text_hash`;
  - ограничение длины пользовательских сообщений до 4000 символов на уровне
    базы.

## Iterations

1. Схема и модели
   - Оставить модель `Chunk` для страниц и базы знаний.
   - Добавить embedding-поля в модель `ChatMsg`.
   - Добавить Alembic-миграцию:
     - добавить embedding-поля и ограничение длины пользовательских сообщений в
       `chat_msg`;
     - удалить старые строки `chunk.msg_id IS NOT NULL`;
     - удалить chat-поля и chat-HNSW index из `chunk`.

2. KB-пути
   - Проверить crawler, embedder pending queue, API update path, source
     enrichment и project views: они должны работать только с `Chunk` как с
     таблицей страниц.
   - Сохранить текущие RAG-поля и `used_chunks` contract.

3. Chat embedding path
   - Убрать фоновую задачу добавления векторов для сообщений.
   - Вектор пользовательского сообщения строится на этапе подготовки RAG
     контекста, где embedding запроса уже нужен для retrieval.
   - Пользовательское сообщение сохраняется в `chat_msg` уже с `embedding`,
     `text_hash`.
   - Для assistant/system сообщений embedding не сохраняется: для них в текущем
     контексте достаточно `tail_messages`.

4. Retrieval behavior
   - Оставить `tail_messages` как источник истории чата.
   - KB vector search должен обращаться только к `chunk`.
   - Chat vector search должен читать `chat_msg.embedding`.

5. Tests and cleanup
   - Обновить тесты под новые модели и SQL.
   - Добавить регрессионную проверку, что `chat_msg` не дублируется в
     `chunk`.
   - Добавить проверку, что пользовательское сообщение сохраняется с
     embedding-полями в `chat_msg`.
   - Проверить миграционную схему и отсутствие старых обращений к `Chunk` для
     сообщений.

## Verification Criteria

- `venv/bin/pytest tests/test_migration_schema.py`
  - база на Alembic head;
  - модельные колонки присутствуют в базе.
- Точечные тесты embedder:
  - сохранение пользовательского сообщения пишет embedding-поля в `chat_msg`;
  - фоновая индексация сообщений не создает строки в `chunk`;
  - pending KB chunks считает только `chunk`.
- Retrieval tests:
  - KB vector/full-text retrieval возвращает прежние source payloads;
  - RAG context использует `tail_messages`;
  - проверить, используется ли chat semantic retrieval реально; если да, он
    читает `chat_msg.embedding`, а не `chunk`.
- Diff checks:
  - `rg -n "msg_id|chat_id" jobs vchat | rg "\\bChunk\\b|\\bchunk\\b"`
    не показывает смешивания chat-полей с page chunks.
  - `rg -n "msg_id" vchat jobs tests` показывает только `chat_msg` и миграцию
    удаления старых производных данных.
- Локальная проверка данных после миграции:
  - count page chunks до/после совпадает для строк `page_id IS NOT NULL`;
  - `chat_msg` count не меняется.
  - старые строки `chunk.msg_id IS NOT NULL` удалены.
  - новые user-сообщения сохраняются с `chat_msg.embedding IS NOT NULL`.

## Guard Rails

- Не трогать удаленные серверы без отдельного явного разрешения.
- Не добавлять fallback, legacy wrapper или совместимый alias `Chunk` без
  явного согласия.
- Не сохранять сообщения в `chunk`.
- Не создавать message chunks.
- Не выносить embeddings сообщений в отдельную таблицу.
- Не хранить модель embedding в `chat_msg`: модель одна базовая на проект, и
  смешивание моделей недопустимо.
- Не добавлять индекс по векторам сообщений в рамках этой задачи.
- Не менять public widget/API поведение, кроме внутренних таблиц индексации.
- Не менять стратегию KB chunking/reranking сверх необходимого переезда таблицы.
- Не читать `docs/` как рабочую память.
- Python tooling запускать через `venv/bin/...`.

## Resolved Decisions

- Chat semantic retrieval сохраняется как существующая ветка retrieval и читает
  `chat_msg.embedding`.
