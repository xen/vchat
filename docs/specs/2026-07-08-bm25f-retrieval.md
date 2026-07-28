# Task: Спроектировать настоящий BM25F для retrieval

## Goal

Full-text слой retrieval использует настоящий, проверяемый BM25F/BM25-style
scoring для page-specific chunks на основе `pg_search`. Старый PostgreSQL
FTS/`ts_rank_cd` lexical path удаляется из production runtime: lexical rank
перед RRF/rerank должен идти только через `pg_search` BM25 indexes на `page` и
`chunk` с полевыми весами, статистикой корпуса и диагностируемым breakdown.

## Context

- `vchat/views/chat/ctx.py`: `fulltext_supply()` сейчас использует
  `ts_rank_cd(c.fts, websearch_to_tsquery(...))`, `kind_rank` и ручную сортировку
  по `kind_rank`, `text_rank`, `c.id`.
- `vchat/models/data.py`: текущий `Chunk.fts` хранит `tsvector`, который
  собирается из `Page.title`, `Chunk.header_text`, `Chunk.section_path`,
  `Chunk.entity_terms` и `Chunk.text`.
- `migrations/versions/h0i1j2k3l4m5_squashed_initial_schema.sql`: trigger
  `update_chunk_fts()` задает веса PostgreSQL FTS, но это не полноценный BM25F.
- `jobs/crawler/tasks.py`: materialization создает chunks с `kind`,
  `header_text`, `section_path`, `entity_terms`, `token_count`, offsets и text.
- `tests/rag_quality/` и `tests/chat/`: текущие retrieval/eval тесты задают
  ожидаемое поведение RAG и должны стать источником регрессионных сценариев.
- AskRavo technology notes по `Hybrid BM25 + Vector Search` и `LightGBM
  Learning-to-Rank Reranker`: production-friendly shape - two-stage
  BM25/vector candidates -> RRF -> rerank; LightGBM/LTR - следующий supervised
  слой после накопления labels и feature logging.
- Цель задачи - перейти на `pg_search` / ParadeDB как production lexical layer.
  Старый PostgreSQL FTS упоминается только как текущее состояние до миграции и
  должен быть убран из production chat retrieval.
- Внешние источники для стартовых параметров: BM25 обычно стартует с `k1=1.2`,
  `b=0.75` в search engines вроде Elasticsearch/OpenSearch; Xapian использует
  более консервативные defaults `k1=1`, `b=0.5`; RRF paper и практические
  материалы обычно стартуют с `k=60`.

## Current Behavior

- Full-text retrieval сейчас работает через PostgreSQL FTS и `ts_rank_cd`; это
  поведение должно быть заменено, а не сохранено в runtime.
- Веса полей частично зашиты в `tsvector`: title/header получают больший вес,
  section/entity terms средний, text меньший.
- Дополнительная сортировка поднимает tables и summaries через `kind_rank`.
- В коде нет отдельного BM25F scorer-а, статистики по полям, нормализации длины
  поля, параметров `k1`/`b`, field boosts или объяснимого score breakdown.
- Vector retrieval и full-text retrieval объединяются позже через RRF и rerank,
  поэтому новый lexical score должен сохранить общий retrieval contract.

## Target Shape

### Архитектурное решение

- Целевой путь строится на `pg_search` / ParadeDB BM25 внутри PostgreSQL:
  `source filter -> pg_search BM25 query -> top N page chunks -> RRF/rerank`.
- Менять основной PostgreSQL как продукт/сервер и выносить поиск во внешний
  search service нельзя. Устанавливать и использовать поддерживаемую зависимость
  `pg_search` можно и нужно, если она дает настоящий BM25 и приемлемый query
  plan.
- Собственную BM25F-формулу в application code держим не как основной runtime
  путь, а как контрольный reference/eval scorer или тонкий post-rank слой, если
  `pg_search` не выражает нужные field boosts / diagnostics напрямую.
- Старый PostgreSQL `tsvector`/GIN + `ts_rank_cd` path удаляется из lexical
  production runtime после cutover. Для сравнения качества его можно запускать
  только в offline/eval harness до удаления.
- Score считается на grain `page_chunk` / текущий page-specific chunk. Будущий
  `embedding_unit` может владеть embeddings, но BM25F должен учитывать
  page-specific поля и `page_id`.

### pg_search BM25F contract

Минимально приемлемый контракт:

- сделать два field-aware BM25 index-а: document-level index на `page` и
  passage-level index на `chunk`;
- задать field weighting так, чтобы title/header/entity signals реально влияли
  на rank, а не были только склеены в один body;
- source/widget filtering должен быть частью indexed query / WHERE до
  раскрытия результатов;
- score и rank должны попадать в retrieval trace для eval/debug.

Если `pg_search` дает только BM25 по колонкам без полноценного BM25F breakdown,
принимаем pragmatic BM25F-style контракт:

- `pg_search` отвечает за быстрый BM25 candidate generation и основной lexical
  rank;
- field boosts задаются структурой page/chunk indexes или явной weighted field
  strategy;
- отдельный lightweight post-rank scorer может пересчитать field contribution
  только на top `N`, если это нужно для диагностики или качества.

Reference formula для тестов и offline-оценки:

```text
score(d, q) = sum over query terms t:
  idf(t) * ((k1 + 1) * F(t, d)) / (k1 + F(t, d))

F(t, d) = sum over fields f:
  weight_f * tf(t, f, d) / ((1 - b_f) + b_f * len(f, d) / avg_len(f))

idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
```

Стартовые field weights до eval-тюнинга:

| Field | Weight | `b_f` | Reason |
| --- | ---: | ---: | --- |
| `page.title` | 4.0 | 0.20 | Короткое сильное поле, title match часто должен выигрывать. |
| normalized URI slug | 0.8 | 0.10 | Полезно для кодов/страниц, но не должно перебивать текст. |
| `chunk.header_text` | 3.0 | 0.30 | Заголовок секции ближе к intent, чем body. |
| `chunk.section_path` | 2.0 | 0.30 | Иерархия документа важна, но повторяет заголовки. |
| `chunk.entity_terms` | 2.5 | 0.20 | Сжатые термины/сущности должны помогать exact/entity queries. |
| `chunk.text` | 1.0 | 0.75 | Основной body с классической BM25 length normalization. |

- `k1 = 1.2` и `b = 0.75` как стартовый BM25 default, если `pg_search`
  позволяет управлять этими параметрами. Если не позволяет, параметры фиксируем
  как часть `pg_search` contract и тюним field weights / index strategy.
- Для коротких structured fields используем меньший `b_f`, чтобы length
  normalization не переусиливала случайные короткие поля. Если `pg_search` не
  поддерживает per-field `b_f`, это остается в reference scorer/eval, а runtime
  компенсирует веса через page/chunk index design.
- `kind_rank` не должен подменять BM25F. Если нужен boost для `table`,
  `summary`, `code` или `list`, он оформляется как отдельный post-BM25F feature
  с явным весом и eval-проверкой.
- Query terms должны извлекаться тем же tokenizer/analyzer contract, что
  используется `pg_search` index/query. Разнобой tokenizer-ов считается дефектом
  implementation.

### Canonical data and indexes

Начинать надо от существующей модели данных, а не с новой дублирующей таблицы.

Канонические владельцы данных:

- `page` владеет page-level документом: `uri`, `source_id`, `title`, полный
  extracted Markdown в `content`, raw bytes в `raw_content`, `raw_content_type`,
  `raw_content_size`, content hash, `content_value`, crawl/status metadata.
- `chunk` владеет passage-level нарезкой: `page_id`, `chunk_ix`, offsets,
  `kind`, `header_text`, `section_path`, `entity_terms`, `token_count`, `text`,
  `text_hash`, duplicate state и embedding.
- `source` владеет source title/config/rules; его не надо копировать в search
  rows, пока query plan позволяет фильтровать через `page.source_id`.

Целевая структура без лишнего дублирования:

- `pg_search` BM25 index на `page`:
  - indexed fields: `title`, normalized URI/slug, `content`;
  - filter/sort fields: `source_id`, `status_error`, `content_value`;
  - назначение: page-level recall, title/URI/full Markdown matches, page boost.
- `pg_search` BM25 index на `chunk`:
  - indexed fields: `header_text`, `section_path`, `entity_terms` как text,
    `text`;
  - filter fields: `page_id`, `kind`, `is_duplicate`;
  - назначение: passage-level candidates and chunk score.
- Небольшие derived/generated fields допустимы, если `pg_search` не умеет
  удобно индексировать выражения:
  - `page.uri_slug`;
  - `chunk.entity_terms_text`;
  - optional field length diagnostics.

Что не надо дублировать в первой версии:

- не копировать `page.content` в отдельную search table;
- не копировать `chunk.text` в отдельную search table;
- не размножать `page.title`, `uri`, `source_id`, `content_value` в каждую
  chunk-search строку без доказанной необходимости;
- не хранить raw bytes или полный Markdown вне `page`.

Отдельная `chunk_search` projection table допустима только как performance follow-up,
если `EXPLAIN` покажет, что `pg_search` по `chunk` + join/filter по `page`
не использует индекс или не укладывается в latency budget. В таком случае
она должна хранить только минимальный denormalized search contract, а не
становиться вторым источником истины и не копировать raw/full Markdown.

### Retrieval strategy motivation

RAG-контекст должен собираться из passage-level chunks, а не из full page rows:
prompt получает ограниченные фрагменты с offsets, section/header metadata,
source cards и понятным provenance. Поэтому `chunk` BM25 query является основным
lexical path и прямой заменой текущего `fulltext_supply()` / `ts_rank_cd`
retrieval.

`page` BM25 query нужен как document-level вспомогательный сигнал, а не как
источник текста для prompt. Он закрывает случаи, где intent лучше виден на
уровне документа: title match, URI/slug, редкий термин в полном Markdown,
страница с хорошим page score, но без сильного отдельного chunk hit. Такой page
hit должен давать boost/expansion для chunks этой страницы, после чего в RAG
context все равно попадают только bounded chunks.

Это соответствует обычной production-схеме RAG: быстрый passage retrieval дает
основные candidates для контекста, document-level retrieval улучшает recall и
стабильность на title/URI/full-document запросах, затем vector/BM25 candidates
смешиваются через RRF/rerank. Практический порядок внедрения:

1. Сначала заменить old lexical path на `pg_search` chunk query.
2. Проверить качество/latency chunk-only lexical path в hybrid RRF.
3. Затем добавить page query как boost/expansion слой, если eval показывает
   пользу для title/URI/full-page scenarios.

### Crawler pipeline changes

`pg_search` должен индексировать канонические rows, которые crawler уже
поддерживает: `page` и `chunk`.

Page-level процесс:

- `jobs/crawler/pipelines.py` уже сохраняет extracted Markdown в `page.content`,
  raw payload в `page.raw_content` / `raw_content_type` / `raw_content_size`,
  title, hash, content value и status.
- После `session.flush()` page row должна быть достаточной для page BM25 index:
  отдельный sync helper не нужен, если index построен напрямую на `page`.
- Если нужны derived fields (`uri_slug`, normalized title), они должны
  обновляться там же, где обновляются `page.uri`/`page.title`, и быть частью
  page canonical row, а не отдельной projection.
- Low-content, too-big, duplicate-page и no-content состояния не должны
  удалять `page`: page остается audit/source-of-truth row, но lexical query
  фильтрует `status_error IS NULL` и `content_value`.

Chunk-level процесс:

- `materialize_page_chunks()` остается владельцем создания passage rows.
- После `session.flush()`, `reuse_existing_chunk_embeddings()` и
  `mark_duplicate_page_chunks()` никаких отдельных search rows создавать не
  надо: BM25 index на `chunk` обновляется вместе с INSERT/UPDATE/DELETE chunk
  rows.
- Если нужен `entity_terms_text`, он должен быть generated column или обычное
  derived поле на `chunk`, обновляемое при создании chunk вместе с
  `entity_terms`.
- При `not chunks`, `mark_page_embedder_failed()`, `mark_page_too_big()`,
  `refresh_project_index()` / `refresh_source_index()` и прямых delete paths в
  `jobs/crawler/pipelines.py` достаточно удалить/обновить `Chunk`; отдельной
  projection cleanup быть не должно в первой версии.
- `page_chunks_match_current_content()` должен дополнительно проверять index
  contract version только если новая схема добавляет derived fields на
  `page`/`chunk`. Если BM25 index строится только по уже существующим columns,
  current chunks можно skip-ать как сейчас.

Query shape:

- Primary query: `chunk_hits` через `pg_search` по `chunk.header_text`,
  `chunk.section_path`, `chunk.entity_terms_text`, `chunk.text`, с join к `page`
  для source/widget/status/content filters и `chunk.is_duplicate = false`.
  Именно этот query заменяет current `fulltext_supply()` в RAG context path.
- Auxiliary query: `page_hits` через `pg_search` по `page.title`,
  `page.uri_slug`, `page.content`, с source/widget/status/content filters.
  Этот query не кладет full page text в prompt; он только усиливает или
  расширяет chunk candidates.
- `lexical_candidates` строятся от chunks:
  - chunks from `chunk_hits` получают основной passage BM25 score;
  - chunks, чьи pages есть в `page_hits`, получают page-level boost;
  - если page hit не дал chunk hit, выбрать ограниченное число chunks этой page
    по `chunk_ix`, `kind`, heading/entity overlap или lightweight local match;
  - page-only result без выбранного chunk не должен попадать в RAG context.

Такой двухуровневый path использует `page` как document source of truth и
`chunk` как passage source of truth, не создавая третью копию Markdown/chunk
text.

Начальный BM25 index sketch:

```sql
CREATE EXTENSION IF NOT EXISTS pg_search;

CREATE INDEX ix_page_bm25 ON page
USING bm25 (
    id,
    (title::pdb.simple('alias=title')),
    (uri_slug::pdb.ngram(3, 5, 'alias=uri_slug')),
    (content::pdb.simple('alias=body')),
    source_id,
    status_error,
    content_value
)
WITH (key_field = 'id');

CREATE INDEX ix_chunk_bm25 ON chunk
USING bm25 (
    id,
    (header_text::pdb.simple('alias=header')),
    (section_path::pdb.simple('alias=section')),
    (entity_terms_text::pdb.simple('alias=entities')),
    (text::pdb.simple('alias=body')),
    (kind::pdb.literal),
    page_id,
    is_duplicate
)
WITH (key_field = 'id');
```

Фактический SQL надо сверить с установленной версией `pg_search`: в документации
0.20+ синтаксис новый, поддерживает `USING bm25`, `key_field`, касты
`pdb.simple` / `pdb.ngram` / `pdb.literal`, `pdb.score(id)` и query boosts.
Если выражения/derived fields нельзя индексировать в нужном виде, добавить
узкие generated/stored columns на `page` и `chunk`, а не отдельную копию
контента.

Предпочтительный scope corpus statistics:

- default: глобально по проектному корпусу / всей indexed collection;
- source filter применяется до раскрытия результатов и внутри candidate
  selection, но IDF не пересчитывается на каждый allowed source scope;
- per-source или per-widget IDF не делать на первом шаге: на маленьких source
  scopes IDF становится нестабильным, scores плохо сравнимы между источниками,
  а runtime/cache сложнее без очевидной пользы.

Это "приятный" production tradeoff: security/visibility решается фильтрами, а
ranking statistics остаются стабильными и дешевыми. Если eval покажет, что
глобальный IDF ухудшает маленькие изолированные источники, отдельной задачей
можно добавить source-local correction или stats scope.

### Runtime path and performance

`pg_search` BM25 не должен сканировать весь корпус на chat request.

Целевой runtime:

1. Построить `pg_search` query из пользовательского текста.
2. Применить source/widget filter до candidate retrieval.
3. Получить top `200-500` lexical candidates через `pg_search` BM25 index.
4. Опционально посчитать lightweight field breakdown/post-rank только для этих
   candidates.
5. Вернуть lexical rank в существующий RRF/rerank pipeline.

Ожидаемый overhead небольшой: основной поиск должен делать `pg_search` index, а
дополнительная application-side работа выполняется только над малым candidate
set. Для reference/post-rank breakdown оценка остается порядка
`query_terms * candidates * fields`: 8 term, 300 candidates и 6 fields дают около
14 400 простых операций плюс чтение готовых page/chunk/stat rows. Основной риск
не формула, а плохой index/query layout, N+1 чтение diagnostics, JSONB-разбор
на каждый candidate или query plan, который не использует `pg_search` index.

Performance budget для первой production-готовой версии:

- extra p95 latency lexical path после перехода <= 30 ms относительно текущего
  production path на локальном representative corpus для top 300 candidates;
- hard fail / redesign signal: > 75 ms p95 или query plan с full scan по
  chunk/page corpus на типовых запросах;
- `pg_search` candidate `EXPLAIN` должен показывать индексный путь или другое
  явно объясненное bounded поведение;
- diagnostics/post-rank scorer должен batch-fetch candidate features
  одним-двумя SQL запросами, без per-candidate SQL.

### RRF and later LTR

- `pg_search` BM25/BM25F-style rank заменяет lexical ranking signal внутри
  hybrid retrieval, но не отменяет vector search, RRF и rerank.
- RRF стартует с `k=60`; веса сначала держать conservative:
  `vector=0.6`, `bm25f=0.4`, затем тюнить на `nDCG@10`, `Recall@10`, `MRR`.
  Для exact/entity-heavy queries можно исследовать adaptive weighting отдельной
  задачей.
- LTR/LightGBM LambdaMART не входит в первую BM25F-реализацию. Его нужно
  готовить как следующий слой после:
  - feature logging для top candidates;
  - golden/eval dataset с query groups и labels `0-3`;
  - baseline matrix: vector only, BM25F only, hybrid RRF, hybrid RRF + rerank;
  - доказанного выигрыша held-out `nDCG@5/10` без недопустимой latency.
- Уже сейчас lexical implementation должен логировать features так, чтобы позже
  LTR мог использовать `pg_search_score` / `bm25f_score`, field contributions
  при наличии, vector distance, RRF rank, chunk kind, field lengths, source
  metadata и position features.

## Guard Rails

- Не выносить поиск во внешний search service и не менять основной PostgreSQL
  как продукт/сервер.
- `pg_search` / ParadeDB разрешен и является предпочтительной опорой для
  lexical BM25 path. Если установка/миграция зависимости нужна, она должна быть
  явной частью implementation plan.
- Не смешивать BM25F-задачу с миграцией `embedding_unit` / `page_chunk`, кроме
  явного учета будущей page-specific модели.
- Не менять crawler extraction/chunking качество в этой задаче.
- Не выполнять тяжелую индексацию, пересчет DF/avg length или LTR inference в
  web request.
- Не расширять source scope для виджетов и публичного чата. Отсутствие binding
  к source остается ошибкой конфигурации, а не разрешением искать по всему
  проекту.
- Не оставлять production path на старый ranking. После cutover `ts_rank_cd` не
  должен участвовать в chat retrieval runtime.
- Не называть `ts_rank_cd` BM25F. `ts_rank_cd` может быть только offline
  comparison tool до удаления старого path.
- Не называть `pg_search` полноценным BM25F, если фактически используется
  простой BM25 по одному склеенному полю без field weighting или field-aware
  index structure.
- Не добавлять LightGBM в runtime до появления labels, offline eval и отдельного
  решения по LTR.

## Iterations

1. **Зафиксировать `pg_search` BM25/BM25F-контракт.**
   - Описать page/chunk indexes, indexed fields, field weights, tokenizer/query
     contract, grain scoring и required diagnostics.
   - Контрольная точка: есть короткий design note и проверка, что `pg_search`
     score/rank реально зависит от field-aware index structure.

2. **Спроектировать page/chunk BM25 indexes и diagnostics.**
   - Зафиксировать, какие existing columns индексируются на `page` и `chunk`,
     какие derived/generated fields нужны (`uri_slug`, `entity_terms_text`,
     field lengths), и какие diagnostics логируются.
   - Контрольная точка: нет отдельной копии `page.content`, `page.raw_content`
     или `chunk.text`; chat request читает готовые `page`/`chunk` indexes.

3. **Встроить derived fields в crawler pipeline.**
   - Если нужны `page.uri_slug`, normalized page fields или
     `chunk.entity_terms_text`, добавить их в модели/миграции и обновлять в тех
     же местах, где crawler пишет `Page` и `Chunk`.
   - Контрольная точка: после crawl/index одной страницы `page`, `chunk`,
     duplicate state и derived fields согласованы в одной transaction; current
     chunks не проходят re-embedding только ради search fields.

4. **Создать `pg_search` BM25 index и query helper.**
   - Добавить migration с `CREATE EXTENSION IF NOT EXISTS pg_search`, BM25 index
     на `page` и BM25 index на `chunk`.
   - Реализовать raw SQL или SQLAlchemy helper для `pdb.score(id)`, field
     boosts, source/widget filters и top-K: `pg_search_chunk_supply()` как
     primary retrieval helper и `pg_search_page_supply()` как auxiliary
     boost/expansion helper.
   - Контрольная точка: `EXPLAIN` показывает использование BM25 index, а не
     sequential scan.

5. **Собрать lexical eval-набор.**
   - Выбрать 20-50 запросов для первого gate: точные названия, номера, таблицы,
     перечисления, редкие термины, цитаты, заголовки и section paths.
   - Контрольная точка: для каждого запроса зафиксированы expected `page_id` или
     URI/source и причина релевантности.

6. **Сделать offline/shadow `pg_search` path.**
   - Реализовать экспериментальный query/helper/eval entrypoint, который
     запускает `pg_search` BM25 на локальной базе без переключения production
     path.
   - Контрольная точка: comparative report current `ts_rank_cd` vs `pg_search`
     BM25/BM25F-style rank на одном eval-наборе, включая field/index
     diagnostics.

7. **Перевести production retrieval на `pg_search`.**
   - Подключить `pg_search` chunk-level lexical rank в `fulltext_supply()` /
     adjacent retrieval helper как единственный lexical production path.
   - Page-level query подключать как boost/expansion слой после chunk-only gate
     или сразу, если eval показывает явную пользу без latency/regression риска.
   - Сохранить source filtering, payload shape, RRF/rerank contract.
   - Контрольная точка: chat retrieval возвращает прежние payload fields, но
     lexical candidates ранжируются `pg_search` BM25; trace показывает
     scorer/index version.

8. **Удалить старый lexical ranking path.**
   - Удалить production-код, который ранжирует chat retrieval через `ts_rank_cd`.
   - Удалить неиспользуемые FTS trigger/index/model assumptions, если они больше
     не нужны для других задач.
   - Контрольная точка: в production chat retrieval нет branch, который может
     вернуться к старому PostgreSQL FTS ranking.

9. **Подготовить LTR как отдельную будущую задачу.**
   - Добавить feature logging и dataset shape, но не обучать модель в рамках
     BM25F/pg_search-задачи.
   - Контрольная точка: можно выгрузить query-candidate groups для будущего
     LightGBM LambdaMART spike.

## Verification

- **Критерии успеха**
  - Lexical score/rank идет через `pg_search` BM25 index по field-aware search
    index contract, а не через один `tsvector`.
  - Если runtime называется BM25F, field weighting подтверждено либо
    возможностями `pg_search`, либо page/chunk index strategy, либо
    lightweight post-rank scorer-ом поверх top candidates.
  - Для exact terms, page titles, section/header matches, entity terms и редких
    слов `pg_search` lexical rank поднимает expected `page_id` выше текущего
    результата на eval-наборе.
  - Source/widget filter применяется до candidate retrieval и не допускает
    результатов из чужих источников.
  - `pg_search` indexes строятся по каноническим `page` и `chunk` rows; полный
    Markdown и raw bytes не копируются в отдельную search table.
  - RAG context строится из bounded chunks; page-level hits используются только
    как boost/expansion signal и не вставляют full page content в prompt.
  - Chunk-level `pg_search` query способен заменить текущий `fulltext_supply()`
    даже без page-level query; page query улучшает recall, но не является
    обязательной опорой для базового context construction.
  - `materialize_page_chunks()` обновляет chunk rows и derived chunk fields после
    dedupe marking и до commit.
  - `index_page_inner()` не запускает повторный chunking/re-embedding только
    ради search index, если canonical rows уже current.
  - Ошибочные, too-big, duplicate, deleted и dangling pages/chunks не остаются
    reachable в production lexical query.
  - Новый lexical rank совместим с RRF/rerank и context payload.
  - `EXPLAIN` candidate retrieval не показывает полный scan всего corpus на
    типовых запросах.
  - `pg_search` lexical path p95 укладывается в <= 30 ms extra latency
    относительно текущего production path для top 300 candidates на
    representative local corpus.
  - Retrieval trace содержит scorer/index version и достаточно debug data для
    eval.

- **Критерии неуспеха**
  - Production retrieval после перехода все еще использует `ts_rank_cd`, общий
    weighted `tsvector` или старый PostgreSQL FTS ranking.
  - Реализация называется BM25F, но использует `pg_search` по одному склеенному
    полю без field-aware index structure.
  - Page-level result попадает в RAG context как full page или unbounded text,
    вместо выбора ограниченных chunks.
  - Query plan или diagnostics data access делает полный пересчет score по всему
    corpus на каждый user request.
  - Нельзя объяснить, какие поля/index components повлияли на rank.
  - Изменение улучшает один ручной запрос ценой просадки lexical eval.
  - Новый слой ломает source cards, cache payload, RRF/rerank assumptions или
    виджетные ограничения доступа.
  - Старый FTS path остается reachable из production chat retrieval.

- **Проверки**
  - Unit/contract test для query builder и derived-field builder.
  - Unit-тесты crawler sync:
    `materialize_page_chunks` writes required derived chunk fields,
    crawler pipeline writes required derived page fields,
    cleanup paths keep lexical query from returning excluded chunks/pages.
  - SQL/data checks: page/chunk coverage, field lengths, index versions.
  - Integration/eval тесты в `tests/rag_quality` для lexical cases.
  - `EXPLAIN` для `pg_search` candidate query с source/widget filter.
  - Runtime measurement для top 100/300/500 candidates.
  - Comparative report: current `ts_rank_cd` vs `pg_search` BM25 vs
    hybrid RRF.
  - Shadow-mode trace review: payload shape прежний, scorer/rank/debug fields
    доступны только для eval/debug.

## Open Questions

- Какой exact `pg_search` page/chunk index contract выбрать: отдельные поля с
  weights, weighted fields или комбинированный вариант?
- Какие параметры `pg_search` доступны в нашей версии зависимости: `k1`, `b`,
  field weights, score explanation, highlighting/facets?
- Нужны ли собственные term-frequency/stat tables только для diagnostics и
  будущего LTR, если runtime score полностью идет через `pg_search`?
- Нужен ли URI slug в первой версии page index, или оставить его за feature flag
  до eval?
- Какой exact gate считать достаточным для cutover: например `pg_search BM25 >=`
  текущего результата по `nDCG@10`, улучшение exact/entity subset и p95 latency
  <= 30 ms на двух последовательных eval runs?
- Какие features логировать сразу для будущего LTR, чтобы не раздуть runtime
  trace и не раскрыть лишние данные пользователю?
