# Отчет: pg_search для локального vchat Postgres

Дата: 2026-07-10

## Scope

Работа выполнена только на локальной базе vchat:

- Postgres: `postgresql://xen@localhost:5432/vchat`
- PostgreSQL: `18.4 (Homebrew)`, `aarch64-apple-darwin25.4.0`
- Удаленные серверы и тестовый `cdn.okumy.com` не использовались.

Цель этого прогона - поставить `pg_search`, построить полный shadow BM25-индекс
по текущему chunk-корпусу сайта и сравнить качество/скорость с текущим
PostgreSQL FTS path на `chunk.fts` + `ts_rank_cd`.

## Установка

Использован ParadeDB `pg_search` `0.24.1`.

Внешние документы, по которым сверялась установка и SQL API:

- `pg_search/README.md`: расширение поддерживает официальные PostgreSQL 15+ и
  для запуска требует `shared_preload_libraries`, затем `CREATE EXTENSION`.
- ParadeDB Extension docs: для macOS arm64/PostgreSQL 18 есть готовый pkg;
  после установки нужно добавить `shared_preload_libraries = 'pg_search'` и
  выполнить `CREATE EXTENSION pg_search`.
- ParadeDB Create Index / Match / Score / Boost docs: BM25-индекс создается
  через `USING bm25 (...) WITH (key_field='...')`, поиск идет через `|||`
  / `&&&`, score читается через `pdb.score(key_field)`, boost задается
  `::pdb.boost(weight)`.

Что сделано:

```sql
-- /opt/homebrew/var/postgresql@18/postgresql.conf
shared_preload_libraries = 'pg_search'
```

Локальный сервис был перезапущен через `brew services restart postgresql@18`.

Проверка:

```sql
SHOW shared_preload_libraries;
-- pg_search

CREATE EXTENSION IF NOT EXISTS pg_search;

SELECT extname, extversion
FROM pg_extension
WHERE extname IN ('pg_search', 'vector', 'pgcrypto')
ORDER BY extname;
```

Результат:

| extname | extversion |
| --- | --- |
| `pg_search` | `0.24.1` |
| `pgcrypto` | `1.4` |
| `vector` | `0.8.1` |

## Индексация

Создана отдельная shadow projection-таблица:

```sql
public.pg_search_chunk_projection
```

Она денормализует текущие `chunk + page` поля, нужные для page-specific lexical
search:

- `chunk_id`, `page_id`, `source_id`
- `uri`, `title`
- `header_text`, `section_path`, `entity_terms_text`, `body_text`
- `kind`, offsets, `token_count`
- `content_value`, `is_duplicate`, `indexed_at`

Projection построена по всем текущим chunk-строкам с непустым `text`:

| metric | value |
| --- | ---: |
| projection rows | 54 965 |
| searchable rows по runtime-фильтру `not is_duplicate and content_value > 0.1` | 50 127 |
| projection table total size | 186 MB |
| BM25 index size | 72 MB |

DDL индекса:

```sql
CREATE INDEX ix_pg_search_chunk_projection_bm25
ON pg_search_chunk_projection
USING bm25 (
    chunk_id,
    source_id,
    page_id,
    uri,
    title,
    header_text,
    section_path,
    entity_terms_text,
    body_text,
    kind,
    content_value,
    is_duplicate
)
WITH (key_field='chunk_id');
```

Время построения на локальной машине:

- projection table: 3.976 s
- BM25 index: 3.725 s

Размеры индексов:

| index | size |
| --- | ---: |
| `ix_pg_search_chunk_projection_bm25` | 72 MB |
| `ix_pg_search_chunk_projection_page_id` | 520 kB |
| `ix_pg_search_chunk_projection_source_id` | 392 kB |
| `pg_search_chunk_projection_pkey` | 1224 kB |

## Методика сравнения

Benchmark сохранен в:

```text
tmp/pg_search_benchmark_results.json
```

Набор: 50 self-derived lexical cases из локального корпуса:

- 25 запросов по уникальным/почти уникальным `title`
- 15 запросов по `header_text`
- 10 запросов по `entity_terms_text`

Для каждого case expected relevance - попадание страницы из той же группы
`page_id`, откуда взят запрос. Это не human-labeled eval, а быстрый offline gate
для lexical recall/ranking на реальном локальном корпусе.

Обе стратегии запускались с одинаковым фильтром:

```sql
NOT is_duplicate AND content_value > 0.1
```

`pg_search` запрос:

```sql
title ||| q::pdb.boost(4)
OR header_text ||| q::pdb.boost(3)
OR section_path ||| q::pdb.boost(2)
OR entity_terms_text ||| q::pdb.boost(2.5)
OR body_text ||| q
ORDER BY pdb.score(chunk_id) DESC
LIMIT 10
```

Текущий FTS baseline:

```sql
c.fts @@ websearch_to_tsquery('russian', q)
OR c.fts @@ websearch_to_tsquery('english', q)
ORDER BY
  ts_rank_cd(c.fts, websearch_to_tsquery('russian', q))
  + ts_rank_cd(c.fts, websearch_to_tsquery('english', q))
DESC
LIMIT 10
```

Для latency: 7 measured repeats на каждый query после warmup.

## Результаты качества

| engine | cases | Hit@1 | Hit@3 | Hit@10 | MRR@10 | empty results |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `pg_search` | 50 | 0.96 | 0.96 | 1.00 | 0.97 | 0 |
| PostgreSQL FTS | 50 | 0.82 | 0.82 | 0.82 | 0.82 | 8 |

Разбивка по типам:

| engine | type | n | Hit@1 | Hit@10 | MRR | median query ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `pg_search` | title | 25 | 0.96 | 1.00 | 0.970 | 13.327 |
| `pg_search` | header | 15 | 1.00 | 1.00 | 1.000 | 12.938 |
| `pg_search` | entity | 10 | 0.90 | 1.00 | 0.925 | 20.323 |
| PostgreSQL FTS | title | 25 | 0.96 | 0.96 | 0.960 | 2.223 |
| PostgreSQL FTS | header | 15 | 0.80 | 0.80 | 0.800 | 1.197 |
| PostgreSQL FTS | entity | 10 | 0.50 | 0.50 | 0.500 | 0.804 |

Основное качественное отличие: `pg_search` лучше обрабатывает field-aware
запросы по header/entity projection и не дает пустых результатов на этом
наборе. Старый FTS не нашел 8 из 50 expected cases.

## Результаты скорости

| engine | samples | median ms | p95 ms | mean ms | min ms | max ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `pg_search` | 350 | 14.728 | 22.520 | 15.115 | 7.103 | 44.017 |
| PostgreSQL FTS | 350 | 1.602 | 67.294 | 10.179 | 0.408 | 142.596 |

Вывод по скорости:

- PostgreSQL FTS быстрее на медиане для коротких/селективных запросов.
- `pg_search` заметно ровнее по хвосту: p95 22.5 ms против 67.3 ms у FTS.
- На текущем локальном корпусе `pg_search` укладывается в проектный p95 budget
  `<= 30 ms extra latency` для shadow lexical top-10.

## Query plan

`pg_search` sample plan использует BM25 index:

```text
Parallel Custom Scan (ParadeDB Base Scan) on pg_search_chunk_projection
  Index: ix_pg_search_chunk_projection_bm25
  Exec Method: TopKScanExecState
  TopK Order By: pdb.score() desc, chunk_id desc
  TopK Limit: 10
```

FTS sample plan использует текущий GIN:

```text
Bitmap Index Scan on ix_chunk_fts
Sort Key: ts_rank_cd(...) DESC
```

## Ограничения

- Это shadow/offline benchmark. Production retrieval в `vchat/views/chat/ctx.py`
  пока не переключен с `ts_rank_cd` на `pg_search`.
- Projection не подключена к crawler/indexing lifecycle. При изменениях
  `chunk/page` ее нужно перестраивать вручную или отдельной фоновой задачей.
- Eval-набор self-derived, не human-labeled. Он годится как smoke/gate для
  lexical exact/header/entity behavior, но не заменяет RAG quality eval.
- В текущем сравнении не измерялись source/widget-specific filters. Поля
  `source_id` и `page_id` включены в BM25 index, но нужен отдельный gate с
  allowed source scopes.
- `pg_search` здесь корректнее называть BM25 / BM25F-style projection, а не
  полноценный BM25F scorer с per-field `b_f` и explain breakdown.

## Рекомендации

1. Оставить текущую базу в состоянии shadow readiness: `pg_search` установлен,
   extension создан, projection + BM25 index построены.
2. Следующим шагом оформить миграцию/DDL и фоновые задачи обновления
   `pg_search_chunk_projection` при materialize/reindex chunks.
3. До production cutover собрать human-labeled eval на 20-50 запросов и
   добавить source-scope checks.
4. После этого переключать `fulltext_supply()` на `pg_search` как единственный
   lexical runtime path и удалять production `ts_rank_cd` ranking.
