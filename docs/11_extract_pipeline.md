# Extraction Pipeline для vchat

Notion link: n/a

## Назначение документа

Этот документ описывает extraction pipeline, который должен предшествовать chunking и retrieval из [docs/10_automaton_context_and_chunking_migration_plan.md](/Users/xen/Dev/sber/vchat/docs/10_automaton_context_and_chunking_migration_plan.md).

Главное правило:

- сначала строится каноническое нормализованное содержимое документа;
- только потом на его основе materialize-ятся snippets и `Chunk`.

`Chunk` не должен создаваться напрямую в crawler/file ingestion.

## Целевой результат extraction pipeline

На выходе любой источник должен давать:

1. `Document.content`
   - markdown-like canonical form;
   - пригоден для chunking;
   - сохраняет заголовки, списки, таблицы, кодовые блоки и логические секции.

2. `Document.meta.structure`
   - структурированное представление документа;
   - пригодно для админской страницы просмотра структуры;
   - пригодно для построения `header_text`, `section_path`, table-snippets и summary-snippets.

3. `Document.title`
   - нормализованный title документа.

4. `Document.meta.extraction`
   - debug-метаданные extraction pipeline:
   - какой extractor использовался;
   - был ли fallback;
   - сколько таблиц найдено;
   - сколько boilerplate-блоков удалено;
   - degraded mode или нет.

## Область покрытия

Pipeline должен покрывать:

- HTML/web pages;
- PDF;
- DOCX;
- TXT;
- RTF;
- uploaded files;
- crawler-import и manual upload одинаково.

## Инварианты

1. Нет прямого создания `Chunk` во время extraction.
2. Все источники сходятся в единый normalized output format.
3. Если структура не извлечена полностью, это явно фиксируется как degraded mode.
4. Даже в degraded mode результат остается каноническим input для индексатора.

## HTML pipeline

### Шаг 1. Первичный extractor

Для HTML основным extractor должен быть `trafilatura.extract()` c параметрами:

- `output_format="markdown"`
- `include_formatting=True`
- `include_tables=True`
- `include_images=False`
- `include_links=True`
- `include_comments=False`
- `no_fallback=False`
- `favor_precision=True`

Результат сохраняется как первичный markdown.

### Шаг 2. Fallback

Нужно сравнить:

- размер исходного текста страницы;
- размер извлеченного markdown.

Если результат extraction слишком мал или явно потерял основную часть документа:

- удалить boilerplate-теги через `BeautifulSoup`
  - `header`
  - `footer`
  - `nav`
  - `aside`
- удалить типовые контейнеры меню/сайдбаров/cookie/ads по `class`/`id`;
- применить `jusText` как fallback-очистку основного текста;
- повторно собрать markdown-like output.

### Шаг 3. Глобальное удаление boilerplate через шинглы

Для сайтов с повторяющимися служебными блоками нужен шаг удаления site-wide boilerplate:

- набор `boilerplate_shingles` строится отдельно;
- удаляются 5-граммы, встречающиеся на большинстве страниц сайта;
- дополнительно удаляются строки boilerplate, повторяющиеся буквально.

Этот шаг особенно важен для:

- меню;
- юридических блоков;
- копирайтов;
- repeated CTA;
- одинаковых футеров.

### Шаг 4. Нормализация markdown

Нужно привести результат к единому формату:

- убрать лишние пустые строки;
- сохранить заголовки;
- сохранить списки;
- сохранить таблицы в markdown-формате;
- сохранить code blocks;
- нормализовать whitespace;
- не терять относительный порядок блоков.

## Pipeline для файлов

### Основной принцип

Файлы не должны индексироваться отдельным упрощенным путем.
Они должны проходить через тот же lifecycle, что и web pages:

1. extraction
2. normalization
3. сохранение `Document.content`
4. сохранение `Document.meta.structure`
5. только потом indexing

### Приоритет extractor-ов

1. Для PDF/DOCX использовать `docling`, если он дает структурированный markdown/result.
2. Для TXT использовать прямую нормализацию plain text в markdown-like format.
3. Для RTF использовать extraction с максимальным сохранением структуры.
4. Если rich structure недоступна, явно отмечать `degraded_mode = true`.

### Degraded mode

Если файл не дал таблицы/секции/структуру, это не должно ломать pipeline.
Но должно быть явно отражено в `Document.meta.extraction`.

Пример:

- `degraded_mode: true`
- `reason: "plain_text_fallback"`

## Структурированный output документа

Extraction pipeline должен возвращать не только текст, но и структуру документа.

### Базовый JSON-контракт

```json
{
  "page_id": "string",
  "url": "string",
  "title": "string",
  "cleaned_markdown": "normalized markdown",
  "structured_entities": [
    {
      "type": "heading",
      "level": 1,
      "content": "Текст заголовка"
    },
    {
      "type": "paragraph",
      "content": "Текст абзаца"
    },
    {
      "type": "list",
      "ordered": false,
      "items": ["пункт 1", "пункт 2"]
    },
    {
      "type": "table",
      "content": "| A | B |\\n|---|---|\\n| 1 | 2 |",
      "caption": "Подпись"
    },
    {
      "type": "code",
      "language": "python",
      "content": "print('ok')"
    }
  ],
  "metadata": {
    "word_count": 1234,
    "table_count": 2,
    "fallback_used": false,
    "boilerplate_removed_count": 15,
    "degraded_mode": false
  }
}
```

### Зачем это нужно

Без этого нельзя надежно:

- построить `section_path`;
- построить `header_text`;
- выделить таблицы;
- сделать admin page структуры документа;
- показать пользователю section-aware citations.

## Что нужно сохранять в `Document.meta`

Рекомендуемая структура:

- `meta["structure"]`
  - список структурных блоков документа;
- `meta["extraction"]`
  - extractor name;
  - fallback flags;
  - degraded mode;
  - debug counters;
- `meta["outline"]`
  - компактное дерево заголовков/секций для быстрых UI-рендеров.

## Связь с materialized chunks

Indexing stage из `docs/10` должен использовать не сырой `Document.content`, а:

- `Document.content`
- `Document.meta.structure`
- `Document.title`

Именно из них строятся:

- `summary`
- `section_summary`
- `text`
- `table`
- `table_rows`
- `entity_projection`

## Админская наблюдаемость

Результаты extraction pipeline должны быть доступны в админке.

Нужна отдельная страница документа, где видно:

- title;
- normalized markdown;
- structure tree;
- extracted entities;
- degraded mode;
- debug metadata;
- downstream snippets/chunks.

Это обязательный инструмент приемки качества extraction.

## Изменения в ingestion

### Что убрать

Нужно убрать прямое создание `Chunk` из:

- `jobs/crawler/files_crawler.py`

### Что оставить

Нужно сохранить lifecycle:

- crawler/upload -> `Document`
- затем `jobs.embedder.tasks.index_document`

### Что добавить

Нужно вынести общий extraction/normalization module, который используют:

- web ingestion;
- uploads;
- file crawler.

## Критерии готовности

1. Любой источник сначала дает `Document.content`, а не `Chunk`.
2. В `Document.meta.structure` есть достаточная информация для section-aware chunking.
3. Таблицы сохраняются как структуры, а не разваливаются в plain text.
4. Админка умеет показать структуру документа.
5. `docs/10` может опираться на этот pipeline без дополнительных обходных веток.

## Итог

Extraction pipeline в `vchat` должен стать единой канонической системой нормализации документов.

Только после этого имеет смысл переносить из `automaton`:

- structured chunk types;
- weighted FTS;
- rerank;
- structured citations;
- section-aware admin/document inspection.
