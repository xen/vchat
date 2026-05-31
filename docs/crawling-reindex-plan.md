# План переработки системы кравлинга и реиндексации

## Что менять: общий список

1. Переименовать модель `Document` → `Page` (таблица `page`). Это затрагивает все слои. `Document` в контексте системы — загружаемый пользователем файл, не страница сайта.
2. Убить `SitemapSpider`. Переименовать `GenericSpider` → `GeneralSpider`.
3. `ROBOTSTXT_OBEY = True` в `jobs/crawler/settings.py`.
4. User-agent кравлера (`Dzen-AI/1.0`) — только в `config.yaml`, убрать из `SourceConfig`.
5. `seed_urls.py` заменить на приоритизированную очередь с корзинами.
6. Добавить лог кравлинга и страницу мониторинга.
7. Добавить несколько новых таблиц (см. ниже).
8. Расширить модель `Page` новыми полями.
9. Заменить `sitemaps text[]` на Source отдельной таблицей `Sitemap`.

---

## Модели данных

### Page (переименованный Document)

Добавить поля:

```
http_status         integer        — последний HTTP код (200, 301, 404, 429…)
last_crawled_at     timestamptz    — когда последний раз скачивали
last_modified_at    timestamptz    — когда последний раз менялось содержимое
last_etag           text           — ETag из последнего ответа для условного GET
check_interval_days integer        — адаптивный интервал перепроверки, стартовый 7
stable_count        integer        — сколько раз подряд контент не изменился
error_count         integer        — сколько ошибок подряд (сбрасывается при успехе)
is_hub_page         boolean        — страница-агрегатор, исключается из RAG-сниппетов
content_value       float  0–1     — насколько полезна как результат поиска
inlink_count        integer        — сколько страниц любого источника на неё ссылается
```

`last_etag` используется для условных GET-запросов:
`GET /page HTTP/1.1 If-None-Match: "abc123"` → сервер вернёт 304 если не изменилось.
Это экономит трафик и время для больших страниц с нормально работающим сервером.

Статусы страницы (`status` enum):

| Статус             | Смысл                                                                   |
| ------------------ | ----------------------------------------------------------------------- |
| `added`            | URL добавлен в систему, требует индексации; использовался как сигнал    |
|                    | переиндексации при смене модели эмбеддингов. Заменяется `index_status`  |
| `pending`          | URL известен, ещё не скачивался                                         |
| `ok`               | скачан, содержимое актуально                                            |
| `indexed`          | скачан и успешно проиндексирован в embeddings; legacy-статус,           |
|                    | функционально эквивалентен `ok` + `index_status = 'indexed'`            |
| `unchanged`        | скачан, 304 Not Modified или хэш совпал                                 |
| `error_4xx`        | 404/403/410                                                             |
| `error_5xx`        | серверная ошибка                                                        |
| `blocked`          | 403/429 стабильно                                                       |
| `redirect`         | 301/302, нужно обновить canonical URL                                   |
| `no_content`       | SPA / пустая / нетекстовая страница                                     |
| `excluded_robots`  | запрещено robots.txt                                                    |
| `excluded_rules`   | исключено правилами источника                                           |
| `excluded_auth`    | редирект на страницу авторизации                                        |
| `excluded_ignored` | помечено вручную оператором                                             |

Поле `index_status` (String, не enum) отражает прогресс embedder-пайплайна независимо от `status`:

| Значение    | Смысл                                              |
| ----------- | -------------------------------------------------- |
| `null`      | страница не стоит в очереди на индексацию          |
| `queued`    | поставлена в очередь embedder (заменяет `added`)   |
| `indexing`  | embedder сейчас обрабатывает                       |
| `indexed`   | успешно проиндексировано                           |
| `failed`    | ошибка индексации                                  |

Переход: `null → queued → indexing → indexed / failed`.

Страницы со статусом `excluded_*` или `error_4xx` (устойчиво):
- Немедленно исключаются из поиска: их чанки помечаются как неактивные (или удаляются)
- Они не попадают в retrieval и не участвуют в построении ответов RAG
- Сама запись в таблице хранится до окончательного удаления задачей-уборщиком

### Таблица Sitemap

Заменяет поле `sitemaps text[]` на Source.

```
id                  serial
source_id           fk Source
url                 text
is_excluded         boolean default false
discovered_via      enum(manual, robots_txt, auto_probe)
first_seen_at       timestamptz
last_fetched_at     timestamptz nullable
last_etag           text nullable
last_content_hash   text nullable      — sha256 тела для сравнения без парсинга
url_count           integer nullable   — сколько URL найдено в последнем парсинге
```

При обнаружении нового sitemap (из robots.txt или авто-пробинга) запись добавляется
автоматически с `is_excluded = false`. Если оператор исключает — `is_excluded = true`,
запись остаётся; при повторном обнаружении того же URL запись не создаётся снова.

**Управление через интерфейс** (страница настроек источника, HTMX):
- Список всех sitemap с колонками: URL, откуда обнаружен, `url_count`, дата обращения
- Кнопки: добавить вручную, исключить/включить, удалить
- При добавлении — немедленная проверка доступности (inline, без перезагрузки)
- Данные из `sitemaps text[]` переносятся в новую таблицу миграцией

### Таблица PageLink

Граф ссылок между страницами.

```
id                  bigserial
source_uri          text           — нормализованный URL страницы-источника ссылки
target_uri          text           — нормализованный URL цели
source_doc_id       fk Page nullable
target_doc_id       fk Page nullable
source_id           integer        — denormalized для быстрых запросов
target_status       enum(ok, not_indexed, missing, auth_required, blocked)
found_at            timestamptz
```

**Нормализация URI**: перед сохранением из URL удаляются параметры из правил
источника (UTM-метки и прочие параметры, настроенные в SourceConfig через `param`-правила).
Нормализация применяется и при поиске существующей записи, и при сохранении новой.

Ссылки между разными источниками сохраняются — все сайты в системе являются
допустимыми источниками для перекрёстных ссылок.

`target_status` позволяет отдавать клиенту список страниц, которые ссылаются
на недоступный или закрытый авторизацией контент.

### Правило удаления мёртвых страниц

Страница **удаляется автоматически** если выполняются оба условия:
- `http_status` ∈ {404, 410} при ≥2 проверках с разрывом ≥7 дней
- `inlink_count = 0` (ни одна страница любого источника на неё не ссылается)

Ссылки из sitemap **не считаются** как inlinks — sitemap это манифест,
а не живой контент; устаревший sitemap не удерживает мёртвые страницы.

Страницы из `start_pages` источника автоматическому удалению не подлежат.

Перед физическим удалением: все чанки страницы удаляются, записи в PageLink
очищаются. Физическое удаление — задача-уборщик после каждого кравла источника.

### Расширение SourceConfig

Убрать `crawler_user_agent` из SourceConfig.

Добавить:
```
crawler_max_pages   integer   — лимит страниц на один ран (вычисляется, см. ниже)
```

`crawler_max_pages` вычисляется автоматически из расписания:
```
time_window = секунды до следующего рана по reindex_cron
max_pages = floor(time_window * 0.8 / download_delay)
```
Коэффициент 0.8 оставляет запас на overhead и дает перекрытие.
В Scrapy: `CLOSESPIDER_PAGECOUNT = crawler_max_pages`.

**Сценарий прерванного кравла**: если кравлер остановился на середине и был перезапущен,
страницы уже посещённые в этом ране имеют свежий `last_crawled_at` и не попадут
в очередь снова (их интервал не истёк). Следующий ран получает свежий бюджет
и обходит оставшиеся страницы. CrawlRun незавершённого рана помечается
`exit_reason = 'interrupted'` при старте нового рана для того же источника.

### Таблица CrawlRun

```
id                  serial
source_id           fk Source
started_at          timestamptz
finished_at         timestamptz nullable
pages_crawled       integer default 0
pages_new           integer default 0
pages_changed       integer default 0
pages_errors        integer default 0
pages_excluded      integer default 0
was_rate_limited    boolean default false
exit_reason         text nullable       — 'finished', 'page_limit', 'error', 'interrupted'
notes               text nullable
```

---

## Алгоритм одного кравла

```
1. Найти незавершённые CrawlRun для этого source_id (finished_at IS NULL)
   → Пометить их exit_reason = 'interrupted', finished_at = now()

2. Создать новый CrawlRun, получить lock (source:{id}:crawl)

3. Загрузить robots.txt:
   - Из кэша если он свежее 24 часов (поле robots_cache на Source)
   - Иначе GET {source_uri}/robots.txt, распарсить, закэшировать
   - Из robots.txt извлечь Sitemap: директивы → добавить в таблицу Sitemap

4. Проверить каждый активный (is_excluded=false) Sitemap:
   - GET запрос с заголовком If-None-Match: {last_etag}
     (или If-Modified-Since: {last_fetched_at} если ETag не был сохранён)
   - 304 Not Modified → sitemap не изменился, пропустить
   - 200 OK → сравнить sha256 тела с last_content_hash
     - hash совпал → обновить last_etag, пропустить парсинг
     - hash изменился → распарсить, обновить url_count, last_content_hash, last_etag
       → страницы с изменившимся <lastmod> → повысить приоритет в очереди B

5. Сформировать бюджет и очередь (алгоритм ниже)

6. Обходить страницы из очереди, соблюдая budget
   - Обновлять CrawlRun.pages_* в процессе

7. После завершения:
   - update_inlink_counts_task(source_id)
   - cleanup_orphans_task(source_id)
   - refresh_project_index если были изменения
   - CrawlRun.finished_at = now(), exit_reason = 'finished' (или 'page_limit')
```

### Алгоритм формирования бюджета из корзин

Корзины определяют из чего берутся страницы, но доли **эластичны**:
если корзина не заполнена, её остаток отдаётся другим корзинам.

```
B = crawler_max_pages  (общий бюджет)

# Собрать кандидатов
A = все hub-страницы источника (is_hub_page = true)
B_pages = страницы где (now() - last_crawled_at) >= check_interval_days * interval '1 day'
          + страницы со статусом pending
          + страницы из sitemap с изменившимся lastmod (добавляются первыми)
C_pages = страницы со статусом error_5xx (retry)

# Максимальные доли корзин при полном заполнении
cap_A = floor(B * 0.20)
cap_B = floor(B * 0.60)
cap_C = floor(B * 0.20)

# Фактическое заполнение с учётом доступных страниц
alloc_A = min(len(A), cap_A)
alloc_B = min(len(B_pages), cap_B)
alloc_C = min(len(C_pages), cap_C)

# Перераспределить остаток бюджета в порядке приоритета: B → C → A
remaining = B - alloc_A - alloc_B - alloc_C

if remaining > 0 and len(B_pages) > alloc_B:
    extra = min(remaining, len(B_pages) - alloc_B)
    alloc_B += extra
    remaining -= extra

if remaining > 0 and len(C_pages) > alloc_C:
    extra = min(remaining, len(C_pages) - alloc_C)
    alloc_C += extra
    remaining -= extra

if remaining > 0 and len(A) > alloc_A:
    extra = min(remaining, len(A) - alloc_A)
    alloc_A += extra
    remaining -= extra

# Итоговая очередь (порядок важен для CLOSESPIDER_PAGECOUNT)
queue = (первые alloc_A из A) + (первые alloc_B из B_pages) + (первые alloc_C из C_pages)
```

Сортировка внутри B_pages: страницы с наибольшей просрочкой первыми
(`now() - last_crawled_at - check_interval_days`).

---

## Адаптивный интервал перепроверки

После каждого успешного кравла страницы:

```python
if content_changed:
    stable_count = 0
    check_interval_days = max(1, check_interval_days // 2)
else:
    stable_count += 1
    check_interval_days = min(90, int(check_interval_days * 1.5))
```

Диапазон: 1–90 дней. Стартовое значение: 7 дней.

При `error_4xx` трижды подряд: `check_interval_days = 90`.
При восстановлении страницы (200 после ошибки): `check_interval_days` сбрасывается на 7.

Hub-страницы (`is_hub_page = True`) не адаптируются — они всегда в корзине A.

---

## Определение hub-страниц

Hub-страница — агрегатор: список новостей, каталог, страница пагинации.
Она нужна для обнаружения URL дочерних страниц, но **не должна**
попадать в RAG-сниппеты в качестве результата поиска.

`is_hub_page = True` устанавливается автоматически в pipeline при обработке страницы.
Оператор может переключить вручную через интерфейс.

Следствия `is_hub_page = True`:
- `content_value` ≤ 0.1 автоматически
- Страница не попадает в retrieval (фильтр `content_value > 0.1`)
- Страница всегда в корзине A: обходится при каждом кравле для поиска новых URL
- Страница не удаляется задачей-уборщиком

### Алгоритм определения hub-страницы

**Шаг 1. Очистить страницу от навигации и шаблонных блоков**

Прежде чем считать ссылки или слова — получить "контентную" часть страницы:
исключить блоки, которые boilerplate-детектор пометил как шаблонные
(count/total_docs > 0.4 для данного источника).

Если boilerplate-индекс ещё не построен (первый кравл) — использовать
структурные эвристики Docling: секции с тегами `<nav>`, `<header>`, `<footer>`
обычно исключаются Docling сам по себе.

**Шаг 2. Подсчёт внутренних ссылок в очищенном контенте**

`internal_links` = markdown-ссылки `[text](url)` где URL принадлежит домену источника
или является относительным.

Данные из реальных источников (vbudushee.ru, catalog.vbudushee.ru):
- Контентные страницы: 5–20 внутренних ссылок после очистки
- Hub-страницы: 50–290 ссылок (`/education/` — 280, `/catalog/mladshaya-shkola/` — 163)

Если `internal_links ≥ 40` → `is_hub_page = True`.

**Шаг 3. Дополнительные URL-паттерны**

Применяются только при пограничных значениях (20–40 ссылок).
Список паттернов из реальных данных источников:

```
/table$, /table/$      — таблицы записей (grant.vbudushee.ru)
/cards$, /cards/$      — страницы с карточками
```

Расширяемый список паттернов в SourceConfig. Без реальных данных паттерны
типа `/blog/`, `/news/` не добавлять — они слишком агрессивны.

**Шаг 4. Уточнение по истории**

После нескольких кравлов: если из страницы за один ран обнаружено ≥10 новых URL,
которых раньше не было в PageLink → hub.

---

## Обнаружение авторизационных редиректов

При получении 301/302 проверяем URL назначения.

Признаки auth-редиректа (любой из):
- Сегмент пути: `/login`, `/auth`, `/signin`, `/account/login`, `/user/login`
- Query-параметр: `next=`, `return=`, `redirect=`, `next_url=`
- Финальный URL имеет другой host (редирект на SSO)

При обнаружении → `status = excluded_auth`, контент не сохраняется,
чанки страницы (если были) деактивируются.

---

## robots.txt

```python
ROBOTSTXT_OBEY = True
```

User-agent кравлера берётся из `config.yaml` (ключ `crawler_user_agent`).
Значение `Dzen-AI/1.0` — дефолт в конфиге. Нигде в SourceConfig не хранится.

Сейчас `Dzen-AI` отсутствует в кодовой базе. Нужно:
1. Добавить ключ в `config.yaml`
2. Убрать `DEFAULT_CRAWLER_USER_AGENT` из `source_settings.py` или сделать
   его читающим из config

При запуске кравла:
1. GET `{source_uri}/robots.txt` — только если кэш старше 24 часов
2. Кэш хранится в поле `robots_cache jsonb` на Source: `{rules: [...], fetched_at: "..."}`
   Scrapy использует встроенный механизм robots, но нам нужен кэш для извлечения
   Sitemap-директив без повторного скачивания
3. Из robots.txt извлечь `Sitemap:` директивы → добавить в таблицу Sitemap
   с `discovered_via = 'robots_txt'` если записи ещё нет
4. `Crawl-delay` из robots.txt: `effective_delay = max(config.download_delay, crawl_delay)`

---

## Исключение страниц из индекса

Полный перечень случаев:

| Причина                                   | Статус             | Как определяется                                      |
| ----------------------------------------- | ------------------ | ----------------------------------------------------- |
| Запрещено robots.txt                      | `excluded_robots`  | Scrapy автоматически                                  |
| Редирект на авторизацию                   | `excluded_auth`    | Паттерны URL редиректа                                |
| Правила источника (CSS/XPath/regex/param) | `excluded_rules`   | SourceConfig.rules при совпадении URL или содержимого |
| Вручную оператором                        | `excluded_ignored` | `is_ignored = True`                                   |
| SPA / пустой контент                      | `no_content`       | `extract_url_document` вернул пустой markdown         |
| Стабильно 404 + нет inlinks               | удаляется          | Задача-уборщик                                        |

При назначении любого `excluded_*` статуса:
- Чанки страницы деактивируются немедленно (или удаляются)
- Страница не попадает в результаты поиска
- Запись в таблице сохраняется — оператор видит что исключено и по какой причине

---

## Дедупликация шаблонных блоков

Навигация, хедер, футер, cookie-баннер одинаковы на всех страницах источника
и засоряют FTS и embeddings. Текущая реализация шинглов не работает.

### Что такое блок

Секция в markdown-извлечении: текст между заголовками (`##`, `###`) или отдельный
абзац из ≥3 слов. Docling выдаёт такую структуру естественно.

### Алгоритм

Реализуется в embedder pipeline.

1. При обработке страницы: разбить content на блоки
2. Для каждого блока: нормализовать (lowercase, без пунктуации) →
   вычислить 3-shingle fingerprints (хэш каждой тройки слов)
3. Обновить таблицу `SourceShingleFreq (source_id, shingle_hash, count)`
4. Порог boilerplate: `count / total_pages_in_source > 0.4`
   (per-source, не требует ручной настройки)
5. При построении чанков: boilerplate-блоки не включаются в FTS и embeddings,
   остаются в `Page.content` (сырой контент не трогаем)

Пересчёт порога: каждые 50 новых страниц источника.

---

## Sitemap: условные GET-запросы

Google, Bing и другие поисковики используют условный GET — не HEAD.
HEAD ненадёжен: многие CDN отдают разный ETag для HEAD и GET,
часть серверов не реализует HEAD.

Алгоритм (см. раздел "Алгоритм одного кравла", шаг 4):
- GET с заголовком `If-None-Match: {last_etag}` → 304 если не изменился
- При 200: сравнить sha256 тела с `last_content_hash` для дополнительной проверки
- Обновить `last_etag`, `last_content_hash`, `last_fetched_at`, `url_count`

---

## Rate limiting и anti-blocking

В Scrapy настройки по умолчанию:

```python
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0
AUTOTHROTTLE_MAX_DELAY = 60
```

AutoThrottle автоматически замедляется при задержках сервера.

Дополнительно:
- HTTP 429 + `Retry-After` → соблюдать заголовок
- Если `was_rate_limited = True` в последних 3 CrawlRun подряд →
  удвоить `download_delay` для этого источника (записать в SourceConfig)

---

## Фоновые задачи

### Требования (обязательно для всех задач)

**Идемпотентность**: повторный запуск с теми же параметрами не создаёт дубликатов
и не приводит к неконсистентному состоянию. Реализация: `INSERT ... ON CONFLICT DO UPDATE`,
проверка перед изменением, `UPDATE` вместо `DELETE + INSERT`.

**Горизонтальное масштабирование**: несколько воркеров одного типа работают
параллельно без конфликтов. Реализация: задачи на разные `source_id` идут
на разные воркеры; для одного `source_id` — lock через
`SELECT FOR UPDATE SKIP LOCKED` или Redis-ключ `source:{id}:crawl_lock` с TTL.
При добавлении воркеров в Kubernetes — задачи автоматически распределяются,
повторного кравла одного источника не происходит.

**Атомарность**: задача либо завершается полностью, либо не меняет состояние.
`last_crawled_at` и статус обновляются только после успешной записи контента.

**Observability**: каждая задача пишет JSON-лог с полями `source_id`, `doc_id`,
тип события, результат, duration_ms (см. раздел про логирование).

### Список задач

| Задача                                 | Очередь  | Триггер                                       |
| -------------------------------------- | -------- | --------------------------------------------- |
| `crawl_source_task(source_id)`         | crawler  | schedule_crawl_task или вручную               |
| `schedule_crawl_task`                  | crawler  | Celery Beat каждый час                        |
| `sitemap_sync_task(source_id)`         | crawler  | Celery Beat несколько раз в день              |
| `update_inlink_counts_task(source_id)` | crawler  | После crawl_source_task                       |
| `cleanup_orphans_task(source_id)`      | crawler  | После update_inlink_counts_task               |
| `schedule_index_page(page_id)`         | embedder | После изменения Page.content                  |
| `refresh_project_index`                | embedder | После crawl_source_task при наличии изменений |
| `rebuild_boilerplate_index(source_id)` | embedder | После полного кравла                          |

Цепочка выполнения:
```
schedule_crawl_task
  → crawl_source_task(source_id)
      → update_inlink_counts_task(source_id)
          → cleanup_orphans_task(source_id)
      → refresh_project_index  (если были изменения)
```

---

## Логирование и мониторинг

### Структурированные логи

JSON на каждое событие. Обязательные поля:

```json
{
  "ts": "2025-05-30T12:34:56Z",
  "level": "info",
  "task": "crawl_source_task",
  "source_id": 4,
  "url": "https://...",
  "event": "page_crawled",
  "http_status": 200,
  "content_changed": true,
  "duration_ms": 342
}
```

Типы событий: `crawl_started`, `crawl_finished`, `page_crawled`, `page_skipped`,
`page_error`, `page_excluded`, `sitemap_checked`, `sitemap_changed`,
`robots_fetched`, `rate_limited`, `orphan_deleted`.

### Страница мониторинга кравлинга

Отдельная страница (сейчас её нет, только мониторинг индексации).

Per-source:
- Последний CrawlRun: дата, страниц crawled/new/changed/errors
- Статистика по статусам страниц (таблица: ok / error_4xx / blocked / excluded_* / pending)
- Список Sitemap с url_count (отсюда же управление — добавить/исключить)
- Следующий запланированный ран

Глобально:
- Очередь задач кравлинга
- Последние N событий лога

Отдельный блок: страницы со статусом `excluded_*` с причиной и кнопкой отмены исключения.

### Метрики (Prometheus)

```
crawler_pages_total{source_id, status}
crawler_run_duration_seconds{source_id}
crawler_queue_size{source_id}
crawler_rate_limited_total{source_id}
embedder_queue_size
```

Алерты:
- Источник не кравлился >14 дней → warning
- `was_rate_limited` последние 3 рана подряд → warning
- `exit_reason = 'error'` три раза подряд → critical

---

## Порядок реализации

### Фаза A: критические фиксы

1. `jobs/crawler/settings.py` → `ROBOTSTXT_OBEY = True`
2. `config.yaml` → `crawler_user_agent: "Dzen-AI/1.0"`, убрать из SourceConfig
3. Убить `SitemapSpider`, переименовать `GenericSpider` → `GeneralSpider`
4. Миграция: расширить `status` enum, добавить `http_status`, `last_crawled_at`, `last_etag`
5. `pipelines.py` → писать `http_status`, `last_crawled_at`, `last_etag`,
   определять `excluded_auth`
6. Добавить `CLOSESPIDER_PAGECOUNT` вычисляемый из окна расписания
7. Чанки страниц с `excluded_*` / `error_4xx` деактивируются немедленно

### Фаза B: умный реиндекс

8. Миграция: добавить все новые поля в Page (last_modified_at, check_interval_days,
   stable_count, error_count, is_hub_page, content_value, inlink_count)
9. Миграция: создать таблицу Sitemap, перенести данные из `source.sitemaps`
10. Создать таблицы PageLink, CrawlRun
11. `pipelines.py` → адаптивный интервал, hub detection, обновление PageLink
12. Заменить `seed_urls.py` на очередь с корзинами A/B/C
13. Задачи: `update_inlink_counts_task`, `cleanup_orphans_task`
14. Управление Sitemap через HTMX на странице настроек источника
15. Условный GET для sitemap (If-None-Match)

### Фаза C: качество индекса

16. Таблица `SourceShingleFreq`, boilerplate exclusion в embedder pipeline
17. `content_value` как фильтр в retrieval (исключить hub-страницы из RAG)
18. Страница мониторинга кравлинга
19. Prometheus метрики и алерты
20. Адаптивный `download_delay` на основе истории CrawlRun

### Переименование Document → Page

Отдельный рефакторинг, делать изолированно:
- Таблица `document` → `page`
- Модель `Document` → `Page`
- Все FK, индексы, enum'ы, migrations
- Все ссылки в views, tasks, pipelines, tests

Делать после Фазы A чтобы не смешивать с функциональными изменениями.
