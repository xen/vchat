# Рефакторинг статусов Page

## Цель

Объединить `status` и `index_status` в одно поле. Добавить `status_error`.
Ввести Python Enum классы. Упростить логику во всех сервисах.
Удалить поле `is_ignored` — заменяется на `status_error = excluded_ignored`.

---

## Новые классы

```python
class PageStatus(str, Enum):
    crawler = "crawler"  # ждёт краулера / краулер работает
    parsing = "parsing"  # контент получен, идёт чанкинг и индексация
    ready   = "ready"    # полностью проиндексирован, доступен для поиска


class PageStatusError(str, Enum):
    # Crawler
    http_4xx          = "Клиентская ошибка HTTP"
    http_5xx          = "Серверная ошибка HTTP"
    redirect          = "Страница редиректит на другой URL"
    excluded_robots   = "Заблокировано правилами robots.txt"
    excluded_rules    = "Заблокировано правилами источника"
    excluded_auth     = "Страница ведёт на авторизацию"
    excluded_ignored  = "Исключено вручную"
    extraction_failed = "Ошибка извлечения содержимого"
    no_content        = "Страница не содержит полезного текста"
    low_content       = "Слишком мало содержимого для индексации"

    # Parser / Embedder
    index_failed      = "Ошибка при индексировании"
```

> Все ошибки кроме `index_failed` выставляются в `jobs/crawler/pipelines.py`.
> `index_failed` — в `jobs/embedder/tasks.py`.

---

## Правила

- `status != ready` и `status_error = null` → жёлтый (в процессе)
- `status_error IS NOT NULL` → красный, текст = `status_error.value`
- `status = ready` и `status_error = null` → зелёный (готов)
- В векторный поиск: **только** `status = ready AND status_error IS NULL`
- При наличии `status_error` воркеры документ не трогают до сброса

---

## Переходы статусов

```
Новый документ
  → status = crawler, status_error = null

Краулер скачал страницу
  → status = parsing, status_error = null
  → планируется задача embedder

Краулер получил редирект (3xx)
  → исходная страница: status = crawler, status_error = redirect
  → новая Page для целевого URL: status = crawler, status_error = null

Краулер заблокирован / ошибка / нет контента
  → status = crawler, status_error = <код>

Parser/Embedder завершился
  → status = ready, status_error = null

Parser/Embedder остановился
  → status = parsing, status_error = index_failed

Сброс для переиндексации
  → status = crawler, status_error = null
```

---

## Pipeline widget (три шага)

```
[Crawler]  →  [Parser]  →  [Ready]
```

| status | status_error | Отображение |
|--------|-------------|-------------|
| crawler | null | Crawler жёлтый, остальные серые |
| crawler | есть | Crawler красный, остальные серые |
| parsing | null | Crawler зелёный, Parser жёлтый, Ready серый |
| parsing | есть | Crawler зелёный, Parser красный, Ready серый |
| ready | null | все три зелёные |

---

## Миграция (бэкфилл)

Все записи сбрасываются на `parsing` без `status_error` — embedder подхватит
и переиндексирует. Обратная совместимость не поддерживается.

| Старый status | Новый status | status_error |
|--------------|--------------|------------------------------|
| pending / added | crawler | null |
| error_4xx | crawler | http_4xx |
| error_5xx (http ≥ 500) | crawler | http_5xx |
| error_5xx (extraction_failed / pipeline_failed) | crawler | extraction_failed |
| excluded_auth | crawler | excluded_auth |
| excluded_ignored / is_ignored=true | crawler | excluded_ignored |
| no_content | crawler | no_content |
| low_content | crawler | low_content |
| ok / unchanged (любой index_status) | parsing | null |
| redirect | crawler | redirect |
| blocked | crawler | http_5xx |

> `blocked`, `redirect`, `excluded_robots`, `excluded_rules` в текущей БД отсутствуют
> (никогда не выставлялись). Колонка `index_status` дропается полностью.

---

## Мёртвый код к удалению

- `status` значения: `blocked`, `redirect`, `excluded_robots`, `excluded_rules`, `added`, `indexed`, `ok`, `unchanged`
- Поле `index_status` (колонка + все обращения)
- Поле `is_ignored` (колонка + все обращения) — заменяется на `status_error = excluded_ignored`
- Константы `_ERROR_STATUSES`, `_PENDING_STATUSES`, `_EXCLUDED_STATUSES` в `views.py`
- `index_status = "failed"` — проверялось, но никогда не выставлялось

---

## TODO

- [ ] При **сохранении** regexp-правил источника применять к уже собранным страницам:
      страницы, попадающие под правило, получают `status = crawler, status_error = excluded_rules`,
      контент и чанки удаляются немедленно (убрать из поиска и освободить место).

- [ ] При **удалении** regexp-правила страницы, которые были заблокированы этим правилом
      (`status_error = excluded_rules`), добавляются в очередь на переиндексацию:
      `status = crawler, status_error = null`.

---

## План работ

### 1. Модель и миграция
- [ ] Добавить `vchat/page_status.py` с `PageStatus` и `PageStatusError`
- [ ] Alembic: добавить `status_error VARCHAR`, изменить `status` ENUM (три значения),
      удалить `index_status` и `is_ignored`, бэкфилл по таблице выше

### 2. Crawler (`jobs/crawler/pipelines.py`)
- [ ] Заменить все `page.status = '...'` на `PageStatus` / `PageStatusError`
- [ ] Убрать проверки `page.is_ignored` — заменить на `page.status_error == PageStatusError.excluded_ignored`
- [ ] `redirect`: добавить создание новой Page для целевого URL
- [ ] `excluded_robots` / `excluded_rules`: выставлять при проверке правил перед fetch

### 3. Embedder (`jobs/embedder/tasks.py`)
- [ ] Убрать `if page.status == "low_content": skip` → `if page.status_error: skip`
- [ ] Убрать все обращения к `index_status`
- [ ] При успехе: `status = ready, status_error = null`
- [ ] При ошибке: `status = parsing, status_error = index_failed`

### 4. Views (`vchat/views/`)
- [ ] Убрать строковые константы (`_ERROR_STATUSES` и т.д.), везде `PageStatus` / `PageStatusError`
- [ ] Векторный поиск: `Page.status == PageStatus.ready, Page.status_error.is_(None)`
- [ ] Упростить `_document_pipeline_steps` под три шага
- [ ] Статистика по статусам (progress bars) — пересчитать под новую схему

### 5. Source settings (`vchat/source_settings.py`, `vchat/views/`)
- [ ] При сохранении правил: применить к существующим страницам, удалить контент/чанки
- [ ] При удалении правила: переотправить страницы на переиндексацию

### 6. Фронт (`document_content.html`)
- [ ] Виджет: три шага вместо пяти
- [ ] Текст ошибки: `document.status_error.value` из Enum (не строковые константы)

### 7. Тесты
- [ ] Обновить `tests/test_crawler_overhaul.py`
- [ ] Обновить `tests/test_projects_actions_extended.py`
