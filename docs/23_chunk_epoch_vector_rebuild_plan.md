# Chunk Epoch Vector Rebuild Plan

Status: not implement

Дата: 2026-06-08

## Назначение

Этот документ фиксирует отложенную задачу для следующей версии проекта: ввести
`chunk epoch` и 24/7-safe перестройку большого векторного индекса без остановки
chat runtime.

В текущей версии задача не реализуется. Сейчас добавляются только HNSW индексы на
существующую колонку `chunk.embedding`.

## Проблема

Текущий RAG path ищет по `chunk.embedding` в таблице `chunk`. При росте базы до
сотен тысяч или миллионов векторов обычные `INSERT`, `DELETE` и `UPDATE
embedding` начинают влиять на HNSW index:

- `INSERT` добавляет новую точку в индекс инкрементально;
- `DELETE` помечает строки как удаленные через PostgreSQL MVCC, а физическая
  очистка зависит от `VACUUM`;
- `UPDATE embedding` эквивалентен удалению старой версии и вставке новой;
- при постоянном churn индекс может пухнуть и деградировать по latency/recall;
- `REINDEX CONCURRENTLY` помогает, но все равно создает заметную CPU/IO нагрузку.

Нужно сохранить 24/7 доступность: старый индекс должен обслуживать запросы, пока
новый индекс строится в фоне.

## Целевая модель

В следующей версии ввести три слоя:

1. `base` - крупный immutable-ish слой активной weekly epoch.
2. `delta` - небольшой слой изменений между weekly rebuild.
3. `tombstone` - список chunk ids, удаленных из активного base, но еще физически
   присутствующих до следующей compaction.

Чтение выполняет retrieval по `base + delta`, затем отбрасывает tombstoned ids и
merge/rerank-ит кандидатов.

## Chunk Epoch

`chunk_epoch` - версия снимка, по которому построен большой base index.

Минимальные будущие сущности:

- `chunk_embedding_base_epoch_<N>` или partitioned table для base rows;
- `chunk_embedding_delta`;
- `chunk_embedding_tombstone`;
- `vector_epoch_state`, где хранится активная epoch и состояние rebuild.

Пример state rows:

```text
active_epoch = 41
building_epoch = 42
last_switch_at = 2026-06-08T03:00:00+03:00
```

## Weekly Rebuild Schedule

Перестройка запускается раз в неделю в ночь на понедельник.

Рекомендуемое окно: понедельник, 02:00-05:00 по `Europe/Moscow`.

Этапы:

1. Создать новую epoch `N+1` со статусом `building`.
2. Зафиксировать rebuild snapshot boundary: `snapshot_started_at` и/или high
   watermark по `chunk.updated_at`.
3. В фоне собрать `base_epoch_N+1` из активных chunks:
   - исключить tombstoned chunks;
   - включить неизмененные rows из текущего base;
   - включить актуальные rows из delta;
   - пересчитать embedding только для changed chunks.
4. Построить HNSW index на новой epoch рядом со старой:

```sql
CREATE INDEX CONCURRENTLY ix_chunk_embedding_base_v42_hnsw
ON chunk_embedding_base_v42
USING hnsw (embedding vector_cosine_ops);
```

5. Выполнить validation:
   - count активных chunks;
   - count indexed rows;
   - sample retrieval на контрольных запросах;
   - отсутствие missing embeddings для active chunks.
6. Выполнить короткий catch-up:
   - применить изменения, пришедшие после snapshot boundary;
   - либо оставить их в delta и переключить base с сохранением delta.
7. Горячо переключить active epoch одной транзакцией:

```sql
UPDATE vector_epoch_state
SET active_epoch = 42,
    building_epoch = NULL,
    last_switch_at = now()
WHERE id = 1;
```

8. После grace period удалить старую epoch:

```sql
DROP TABLE chunk_embedding_base_v41;
```

## Runtime Query Contract

Chat retrieval не должен обращаться к hardcoded table/index names. Он должен
читать active epoch из кэша настроек и выполнять:

1. top-K по active base epoch;
2. top-K по delta;
3. исключение tombstones;
4. merge candidates;
5. cross-rerank.

Delta можно обслуживать отдельным небольшим HNSW индексом или sequential scan,
если размер delta ниже установленного порога.

## Churn Policy

Мелкие изменения между weekly rebuild не должны перестраивать большой индекс.

Правила:

- новый chunk попадает в `delta`;
- измененный chunk получает новую row в `delta`, старая base row tombstoned;
- удаленный chunk добавляется в `tombstone`;
- если delta превышает порог, можно запустить внеплановую compaction.

Начальные пороги для будущей реализации:

- rebuild раз в неделю обязательно;
- внеплановый rebuild при delta > 5% от base;
- внеплановый rebuild при tombstones > 10% от base;
- alert при p95 retrieval latency выше SLO в течение 30 минут.

## Operational Notes

- Старый base/index не трогается, пока новый полностью не построен и не
  провалидирован.
- `CREATE INDEX CONCURRENTLY` не останавливает чтение старого индекса, но может
  создавать заметную IO/CPU нагрузку.
- Weekly rebuild должен иметь отдельный rate limit и observability: phase,
  processed rows, remaining rows, index build start/end, validation result.
- На время rebuild новые изменения продолжают попадать в delta.
- Cleanup старой epoch выполняется только после grace period, чтобы не ломать
  долгие запросы, начавшиеся до переключения.

## Current Version Scope

Не реализуется сейчас:

- `chunk_epoch`;
- base/delta/tombstone таблицы;
- weekly rebuild worker;
- hot switch state machine;
- catch-up protocol.

Реализуется сейчас:

- HNSW cosine индексы на текущую `chunk.embedding`, чтобы убрать полный scan по
  векторам в текущем RAG path.
