# Task: Спроектировать BM25F для retrieval

## Goal

После задачи full-text слой retrieval имеет явно выбранный и проверяемый
BM25F-подобный контракт ранжирования. Ранжирование должно учитывать разные поля
страницы и фрагмента с разными весами, работать быстро на пользовательском пути
чата и не маскироваться под BM25F, если фактически используется только
PostgreSQL `ts_rank_cd`.

## Context

- `vchat/views/chat/ctx.py`: `fulltext_supply()` сейчас использует
  `ts_rank_cd(c.fts, websearch_to_tsquery(...))`, `kind_rank` и ручную сортировку
  по `kind_rank`, `text_rank`, `c.id`.
- `vchat/models/data.py`: текущий `Chunk.fts` хранит `tsvector`, который
  собирается из `Page.title`, `Chunk.header_text`, `Chunk.section_path`,
  `Chunk.entity_terms` и `Chunk.text`.
- `migrations/versions/h0i1j2k3l4m5_squashed_initial_schema.sql`: trigger
  `update_chunk_fts()` задает веса PostgreSQL FTS, но это не является
  полноценным BM25F.
- `jobs/crawler/tasks.py`: materialization создает chunks с `kind`,
  `header_text`, `section_path`, `entity_terms`, `token_count`, offsets и text.
- `tests/rag_quality/` и `tests/chat/`: текущие retrieval/eval тесты задают
  ожидаемое поведение RAG и должны стать источником регрессионных сценариев.

## Current Behavior

- Full-text retrieval работает через PostgreSQL FTS и `ts_rank_cd`.
- Веса полей частично зашиты в `tsvector`: title/header получают больший вес,
  section/entity terms средний, text меньший.
- Дополнительная сортировка поднимает tables и summaries через `kind_rank`.
- В коде нет отдельного BM25F-объекта, статистики по полям, нормализации длины
  поля, параметров `k1`/`b`, field boosts или объяснимого score breakdown.
- Vector retrieval и full-text retrieval объединяются позже через RRF и rerank,
  поэтому изменение full-text score не должно ломать общий retrieval contract.

## Target Shape

- Явно выбран подход:
  - либо реализовать настоящий BM25F / BM25F-подобный scoring поверх
    page-specific индексных полей;
  - либо оставить PostgreSQL FTS как быстрый lexical слой и не называть его
    BM25F.
- Если выбран BM25F:
  - определить поля: `page.title`, `page.uri` или normalized slug при
    необходимости, `page_chunk.header_text`, `page_chunk.section_path`,
    `page_chunk.entity_terms`, `page_chunk.text`;
  - определить веса полей и правила нормализации длины;
  - определить, где хранится corpus statistics: document frequency, average
    field length, total indexed units;
  - определить grain scoring: score считается для page-specific chunk/hit, а не
    для канонического `embedding_unit`;
  - сохранить source filtering до scoring или внутри candidate selection так,
    чтобы публичный widget не видел чужие источники.
- Retrieval должен оставаться быстрым: BM25F не должен превращать каждый chat
  request в полный scan всех chunks.
- Score должен быть диагностируемым: для eval/debug можно объяснить вклад
  основных полей без вывода лишних данных пользователю.

## Guard Rails

- Не смешивать BM25F-задачу с миграцией `embedding_unit` / `page_chunk`, кроме
  явного учета будущей page-specific модели.
- Не менять crawler extraction/chunking качество в этой задаче.
- Не выполнять тяжелую индексацию или пересчет corpus statistics в web request.
- Не расширять source scope для виджетов и публичного чата.
- Не добавлять fallback на старый ranking без явного решения; если нужен staged
  rollout, он должен быть описан как временный режим и иметь критерий удаления.
- Не называть текущий `ts_rank_cd` BM25F без отдельного подтверждения.

## Iterations

1. **Зафиксировать BM25F-контракт.**
   - Описать поля, веса, нормализацию длины, параметры `k1`/`b`, grain scoring и
     требуемые corpus statistics.
   - Контрольная точка: есть короткий дизайн scoring formula и список таблиц /
     materialized fields, которые нужны для расчета.

2. **Собрать lexical eval-набор.**
   - Выбрать 20-50 запросов, где lexical retrieval должен выигрывать или
     дополнять vector retrieval: точные названия, таблицы, перечисления, редкие
     термины, цитаты.
   - Контрольная точка: для каждого запроса зафиксированы ожидаемые `page_id`
     или source/URI и причина релевантности.

3. **Спроектировать хранение статистики.**
   - Выбрать, где и когда обновлять DF/average length: при reindex source,
     отдельной фоновой задачей или materialized view.
   - Контрольная точка: массовый reindex не блокирует пользовательский chat path
     и не требует полного пересчета в каждом request.

4. **Сделать прототип ranking без переключения production path.**
   - Реализовать экспериментальный query/helper или notebook-like eval entrypoint
     в `tests/rag_quality` / `jobs`, который считает новый score на локальной
     базе.
   - Контрольная точка: можно сравнить текущий `ts_rank_cd` и новый score на
     одном и том же наборе запросов.

5. **Интегрировать в retrieval.**
   - Заменить или дополнить `fulltext_supply()` новым lexical scorer.
   - Сохранить RRF/rerank contract и source filtering.
   - Контрольная точка: chat retrieval возвращает те же payload fields, но
     lexical candidates ранжируются по новому контракту.

6. **Добавить диагностику и performance gates.**
   - Добавить тесты и `EXPLAIN`/метрики, которые показывают, что запрос не
     деградирует на локальном корпусе.
   - Контрольная точка: есть понятный fail signal для медленного query plan,
     пустой статистики или неожиданного падения lexical eval.

## Verification

- **Критерии успеха**
  - Для точных терминов, названий страниц, section/header matches и редких слов
    BM25F-кандидаты поднимают ожидаемые `page_id` выше текущего baseline.
  - Source filter применяется до раскрытия результатов и не допускает результатов
    из чужих источников.
  - Ranking учитывает разные поля с разными весами, а не только общий
    `tsvector`.
  - Corpus statistics обновляются фоново или при индексации, не в chat request.
  - Новый lexical слой сохраняет совместимость с RRF/rerank и context payload.
  - `EXPLAIN` не показывает полный scan по всему корпусу на типовом запросе без
    явного и приемлемого основания.

- **Критерии неуспеха**
  - Реализация называется BM25F, но использует только `ts_rank_cd`.
  - Score нельзя объяснить или проверить на eval-наборе.
  - Query план требует полного пересчета score по всему корпусу для каждого
    пользовательского запроса.
  - Изменение улучшает один ручной запрос ценой просадки общих lexical eval
    сценариев.
  - Новый слой ломает source cards, cache payload или RRF/rerank assumptions.

- **Проверки**
  - Unit-тест формулы score на небольшом синтетическом корпусе.
  - Integration/eval тесты на локальном срезе `tests/rag_quality`.
  - SQL/data checks для corpus statistics: total units, DF, average field length.
  - `EXPLAIN` для candidate selection и scoring query на локальной БД.
  - Сравнительный отчет: current `ts_rank_cd` baseline vs новый BM25F-кандидат
    на выбранном eval-наборе.

## Open Questions

- Нужен ли настоящий BM25F в production path, или достаточно улучшить
  PostgreSQL FTS и честно называть его lexical retrieval?
- Какие поля должны участвовать в BM25F с какими весами: title, uri slug,
  header, section path, entity terms, text, table rows?
- Считать ли corpus statistics глобально по проекту или отдельно по source /
  allowed source scope?
- Какой performance budget приемлем для full-text candidate generation до RRF и
  rerank?
- Нужно ли сохранять текущий `ts_rank_cd` как fallback на время сравнения, и
  какой критерий позволит его удалить?
