# Задача: подготовленные контекстные пакеты для триггеров виджета

## Цель

Перевести триггеры виджета из набора строк в подготовленные сценарии с
привязанным контекстом страницы.

Сейчас генератор уже передает в LLM реальное содержимое страницы и получает до
10 коротких триггеров. Проблема не в том, что генерация полностью оторвана от
страницы. Проблема в том, что после генерации триггеры сохраняются как простые
строки: при показе и клике они почти не несут с собой подготовленный контекст,
по которому были созданы.

После реализации должно стать верным:

- триггер связан с конкретной индексированной страницей или ее канонической
  версией;
- для триггера заранее подготовлен компактный серверный контекст;
- клиентский API по-прежнему отдает виджету минимальную структуру: текст и
  уникальный код триггера;
- при клике сервер по коду триггера загружает подготовленный контекст и
  использует его внутри;
- ответ по клику не требует онлайн-поиска по глобальной базе знаний;
- LLM для полноценного ответа вызывается только после клика пользователя;
- генерация подсказок до клика использует только дешевую модель, заданную
  отдельным конфигом.

## Загруженный контекст

- `AGENTS.md`: правила проекта, запрет удаленного серверного доступа без явного
  запроса, Python tooling только через `venv/bin/...`.
- `kb/index.md`, `kb/workflow.md`: широкие задачи фиксируются в `specs/`.
- `kb/backend.md`, `kb/invariants.md`: публичный виджет не должен ходить в
  глобальную базу знаний без явного source scope.
- `kb/frontend.md`: `frontend_chat/` и `vchat/templates/js/widget.js` относятся
  к встраиваемому виджету.
- `vchat/templates/js/widget.js`: embed-скрипт запрашивает триггеры и отправляет
  payload при клике.
- `vchat/templates/chat/chat.html`: iframe чата отправляет websocket payload
  `trigger_prompt`.
- `vchat/views/frontend.py`: `/api/triggers/resolve`, discovery страницы через
  виджет, выдача generic и page triggers.
- `vchat/views/chat/views.py`: обработка trigger payload, проверка
  `page_token`, `TriggerResponseCache`, обычный путь генерации ответа.
- `jobs/triggers/generation.py`: генерация 10 триггеров из `Page.uri`,
  `Page.title` и до 8000 символов `Page.content`.
- `jobs/triggers/tasks.py`: `generate_missing_triggers_task`.
- `jobs/crawler/url_rules.py`: нормализация URL через правила источника,
  включая `CrawlerRule(type="param", value="...")`.
- `jobs/crawler/pipelines.py`: определение `low_content`, `too_big`,
  `duplicate_content`, привязка полного дубликата к основной странице.
- `vchat/models/source_config.py`: настройки источника уже содержат правила
  `param`, которые используются при индексации.
- `vchat/models/data.py`: `Page`, `Chunk`, `PageLink`, `PageShingle`,
  `TriggerResponseCache`, `WidgetIntegration`.

## Текущее поведение

### Загрузка виджета

`vchat/templates/js/widget.js` берет URL страницы из
`data-source-page-url || window.location.href` и вызывает специальный API:

```text
GET /api/triggers/resolve?url=<current_url>&title=<document.title>
```

Сейчас endpoint возвращает простую структуру:

```json
{
  "page_token": "...",
  "triggers": [
    {"key": "abc", "text": "Ask about docs"}
  ]
}
```

На клиенте trigger остается минимальным UI-элементом. Пользователь видит только
текст. Служебные поля не должны попадать в интерфейс.

### Generic triggers

Если для страницы нет готовых page triggers, используется список базовых generic
триггеров. Они формируются через `render_triggers(...)` и подставляют
`title` текущей страницы. Этот fallback должен сохраниться.

Пример ответа, когда найден только generic fallback:

```json
{
  "triggers": [
    {"key": "generic-1", "text": "Что важно на странице \"...\"?"}
  ]
}
```

### Discovery через виджет

Если страница не найдена, но источник найден, rules разрешают страницу и
`widget_page_discovery_enabled=true`, текущий механизм создает `Page` с
`discover_by="widget"`, `discover_source=<url>`, `has_triggers=true` и ставит
`crawl_page_task`.

Этот механизм нужно использовать и дальше. Если пользователь попал на новую
страницу, виджет должен помочь поставить ее в очередь индексации, а не
изобретать отдельный путь нормализации и индексации.

### Индексация, параметры и дубликаты

В настройках источника уже есть правила `param`, которые используются
индексатором для игнорируемых query-параметров. Пример:

```json
{
  "rules": [
    {"type": "param", "value": "utm_source"}
  ]
}
```

`jobs/crawler/url_rules.py::normalize_url_for_queue()` удаляет такие параметры
и fragment перед сохранением URL в очередь/страницы. Поэтому задача не должна
добавлять второй независимый механизм игнорирования параметров для виджета.
Нужно переиспользовать ту же семантику source rules.

Если страница является полной копией другой страницы, crawler pipeline помечает
ее как `status_error=duplicate_content` и пишет в `Page.meta`:

```json
{
  "duplicate_of_page_id": 123,
  "duplicate_of_uri": "https://..."
}
```

Если страница тонкая или мусорная, pipeline выставляет `low_content` и удаляет
чанки. Если страница слишком большая, выставляется `too_big`. Эти состояния
должны явно учитываться при выборе триггеров.

Потенциально опасный случай: временные параметры вроде сессионного id или
одноразового access token. Это тоже должно решаться через игнорируемые
параметры в настройках источника, а не через отдельный список в коде виджета.

### Генерация триггеров

Текущий генератор:

- выбирает ready-страницы с `has_triggers=true`;
- пропускает страницы со `status_error`;
- берет `Page.uri`, `Page.title` и `Page.content`;
- обрезает контент до 8000 символов;
- просит LLM вернуть JSON `{"triggers": ["..."]}`;
- чистит результат, оставляет до 10 строк;
- сохраняет их в `Page.triggers`.

Это уже правильная базовая идея: триггеры рождаются из реального контента
страницы. Недостаток: не проверяется явно, достаточно ли информации о странице
передается в LLM для генерации качественных триггеров, и результат не хранит
контекст, из которого эти строки были получены.

### Модель для генерации триггеров

Проверка текущего кода показала: отдельного параметра модели для генерации
триггеров сейчас нет. `generate_trigger_texts_for_page()` использует общие:

```yaml
chat_provider: "openai"
chat_model: "gpt-4o-mini"
```

Для этой задачи нужно добавить отдельные настройки, например:

```yaml
trigger_generation_provider: "openai"
trigger_generation_model: "gpt-4o-mini"
```

Смысл: генерация подсказок выполняется офлайн, массово и платно, поэтому она
должна использовать дешевую модель независимо от модели основного чата.

## Желаемая модель

### Ключевой принцип

Виджет запрашивает простой список триггеров. В ответе клиенту не нужно отдавать
подробный контекст, типы сценариев, chunk ids, источники и служебные решения.

Клиентский контракт должен остаться минимальным:

```json
{
  "triggers": [
    {"key": "t_01H...", "text": "Уточнить сроки участия"}
  ]
}
```

Если есть привязка к странице, endpoint может вернуть `page_token`, но сам
trigger в массиве должен оставаться простым:

```json
{
  "page_token": "signed-page-token",
  "triggers": [
    {"key": "t_01H...", "text": "Уточнить сроки участия"}
  ]
}
```

При клике клиент отправляет только:

```json
{
  "type": "trigger_prompt",
  "page_token": "signed-page-token",
  "trigger_key": "t_01H...",
  "text": "Уточнить сроки участия"
}
```

Дальше сервер по `page_token + trigger_key` загружает подготовленный контекст и
решает, что делать:

- вернуть уже кешированный ответ;
- вызвать LLM с заранее подготовленным контекстом;
- показать generic/fallback ответ, если context package устарел или отсутствует.

### Подготовленный пакет триггера

Нужно добавить серверный derived artifact: пакет подготовленных триггеров для
страницы или канонической страницы.

Минимальная модель:

```text
prepared_trigger_package
- id
- source_id
- page_id
- canonical_page_id
- fingerprint
- status
- built_at
- context_payload
- meta

prepared_trigger
- id
- package_id
- key
- text
- context_payload
- context_chunk_ids
- answer_mode
- sort_order
- created_at
```

`Page` и `Chunk` остаются каноническими данными. Prepared package является
производным артефактом и может быть пересобран.

`context_payload` должен быть типизирован через pydantic-схему. Не нужно
разрастать `Page.meta` и `Page.triggers` произвольными вложенными структурами.

### Что хранить в контексте

Пакет должен хранить ровно то, что понадобится после клика:

- canonical page id;
- исходный page id, если пользователь пришел на дубль;
- source id;
- нормализованный URL;
- title;
- selected chunk ids;
- краткий prepared context из `summary`, `section_summary`, `file_summary` и
  релевантных text chunks;
- список sources для отображения после ответа;
- fingerprint;
- reason code, почему пакет готов или почему fallback.

Для генерации триггеров нужно отдельно проверить, достаточно ли информации
передается в LLM:

- есть ли `Page.content`;
- сколько символов попало в prompt после обрезки;
- есть ли title;
- есть ли summary/section chunks;
- не является ли страница hub/low-value;
- не была ли страница исключена из индексации.

Если информации мало, нужно не генерировать якобы page-specific triggers, а
оставлять generic triggers.

### Ответы endpoint для разных ситуаций

Готовая страница с подготовленными триггерами:

```json
{
  "page_token": "signed-page-token",
  "triggers": [
    {"key": "t_deadline", "text": "Уточнить сроки участия"},
    {"key": "t_plan", "text": "Составить план участия"}
  ]
}
```

Страница неизвестна, но разрешена rules и отправлена в discovery:

```json
{
  "triggers": [
    {"key": "generic_about_page", "text": "Что важно на странице \"Название\"?"}
  ]
}
```

Страница является дублем и использует пакет основной страницы:

```json
{
  "page_token": "signed-canonical-page-token",
  "triggers": [
    {"key": "t_deadline", "text": "Уточнить сроки участия"}
  ]
}
```

Источник отключил триггеры или rules не совпали:

```json
{
  "triggers": []
}
```

Страница тонкая/мусорная и не должна получать page-specific prompts:

```json
{
  "triggers": [
    {"key": "generic_about_page", "text": "Что можно спросить по этой странице?"}
  ]
}
```

Важно: reason codes и подробные статусы полезны для логов и админки, но не
обязаны попадать в публичный клиентский JSON.

## Инвалидация

Пакет устаревает, если меняется что-то из входных данных:

- `Page.hash_value`;
- набор selected chunks или их `text_hash`;
- `duplicate_of_page_id`;
- source rules, включая `param` и `trigger_rules`;
- версия промпта генерации триггеров;
- версия схемы prepared package;
- модель генерации триггеров.

При устаревшем пакете runtime не должен молча использовать старый контекст как
актуальный. Допустимые варианты:

- вернуть generic triggers;
- вернуть пустой список;
- поставить пакет на пересборку;
- показать в админке статус `stale`.

## Кластеризация близких страниц

Кластеры нужны, но не как первая обязательная зависимость. Первый этап должен
работать на уровне canonical page.

После этого можно добавить page cluster для случаев:

- полный дубль;
- near duplicate по shingles;
- страницы одной программы с одинаковыми summary/section структурами;
- варианты одной страницы с разными query-параметрами;
- близкие FAQ/материалы внутри одного источника.

Кластер должен строиться офлайн. Не нужно делать онлайн embedding/search по
клику пользователя ради выбора контекста. Если мы заранее показываем trigger,
то мы заранее должны знать, какой контекст за ним стоит.

Админского ревью кластеров как обязательного шага нет. Но просмотр в админке
желателен: статус пакета, canonical page, использованные chunks, reason code,
fingerprint и дата сборки.

## Итерации

### 1. Привести resolution URL к crawler-семантике

Файлы:

- `vchat/views/frontend.py`
- `vchat/views/triggers/rules.py`
- `jobs/crawler/url_rules.py`
- tests для trigger resolve и URL normalization

Работа:

1. При `widget_triggers_resolve()` использовать нормализацию URL с правилами
   источника, включая `CrawlerRule(type="param")`.
2. Не держать отдельный список ignored params для виджета.
3. Если страница неизвестна, продолжать использовать существующий discovery
   mechanism через `widget_page_discovery_enabled` и `crawl_page_task`.
4. Если найдена duplicate page, переходить к основной странице из
   `duplicate_of_page_id`.
5. Если страница `low_content` или `too_big`, не выдавать page-specific package.

Проверка:

- URL с ignored param резолвится так же, как crawler сохранил бы его в `Page`.
- URL с параметром, не указанным в rules, не теряет этот параметр.
- unknown page при включенном discovery создает `Page` и ставит crawl task.
- duplicate page отдает триггеры основной страницы.

### 2. Добавить отдельный конфиг модели генерации триггеров

Файлы:

- `vchat/config.yaml`
- `jobs/triggers/generation.py`
- tests для trigger generation settings

Работа:

1. Добавить `trigger_generation_provider`.
2. Добавить `trigger_generation_model`.
3. Использовать их в `generate_trigger_texts_for_page()`.
4. Сохранить fail-fast поведение: если provider/model невалидны, генерация
   триггеров падает явно.

Проверка:

- тест доказывает, что генерация триггеров не использует `chat_model`, если
  задан `trigger_generation_model`;
- existing trigger generation tests продолжают проходить.

### 3. Проверить качество входа для генерации триггеров

Файлы:

- `jobs/triggers/generation.py`
- `jobs/triggers/tasks.py`
- tests для отбора страниц

Работа:

1. Добавить явную проверку, что для страницы достаточно данных для генерации:
   title/content/summary или достаточный объем текста.
2. Не генерировать page-specific triggers для низкоценной, пустой,
   исключенной, duplicate или слишком большой страницы без prepared summary.
3. Логировать reason code, почему генерация пропущена.

Проверка:

- страница без достаточного content не отправляется в LLM;
- duplicate/low-content страницы не генерируют собственные triggers;
- страница с нормальным `Page.content` отправляет в LLM реальный контент.

### 4. Добавить prepared trigger package

Файлы:

- `vchat/models/data.py`
- `vchat/models/__init__.py`
- `migrations/versions/...`
- новый модуль для pydantic-схем prepared package
- tests моделей и миграций

Работа:

1. Создать таблицы `prepared_trigger_package` и `prepared_trigger`.
2. Вынести контекст триггера из `Page.triggers` в типизированный derived
   artifact.
3. Хранить public trigger key и text отдельно от внутреннего context payload.
4. Добавить fingerprint.

Проверка:

- пакет нельзя сохранить без страницы или canonical page;
- trigger key уникален внутри package;
- invalid context payload отклоняется до записи;
- удаление страницы инвалидирует или удаляет package по выбранному правилу.

### 5. Собрать package builder для canonical pages

Файлы:

- `jobs/triggers/tasks.py`
- `jobs/triggers/generation.py`
- новый `jobs/triggers/packages.py`
- tests package builder

Работа:

1. Для ready canonical page собрать prepared context из доступных chunks.
2. Приоритет источников контекста: `summary`, `section_summary`,
   `file_summary`, затем ограниченные text chunks.
3. Сгенерировать до 10 trigger records.
4. Сохранить context payload, chunk ids, sources и fingerprint.
5. Не генерировать paid prepared answer офлайн.

Проверка:

- builder не вызывает LLM для ответа, только для генерации подсказок;
- builder использует summary chunks, если они есть;
- context payload ограничен по размеру;
- fingerprint меняется при изменении page hash/chunk hash.

### 6. Использовать package в API выдачи триггеров

Файлы:

- `vchat/views/frontend.py`
- `vchat/templates/js/widget.js`
- tests trigger resolve

Работа:

1. `GET /api/triggers/resolve` ищет готовый package.
2. Если package готов, возвращает только `key` и `text` для каждого trigger.
3. Если package отсутствует, возвращает generic triggers.
4. Если source disabled или trigger rules не совпали, возвращает пустой список.
5. Клиентский UI не меняется визуально.

Проверка:

- ответ ready package содержит только минимальные trigger поля;
- disabled/rules mismatch возвращает `{"triggers": []}`;
- generic fallback использует title страницы;
- публичный JSON не раскрывает chunk ids, context payload или внутренние
  статусы.

### 7. Использовать context package при клике

Файлы:

- `vchat/templates/chat/chat.html`
- `vchat/views/chat/views.py`
- tests websocket trigger path

Работа:

1. При `trigger_prompt` валидировать `page_token` и `trigger_key`.
2. Загружать prepared trigger по server-side id/key.
3. Передавать в LLM prepared context после клика пользователя.
4. Не запускать online vector/fulltext retrieval для первого trigger answer.
5. Сохранять chat messages, sources и full_context так же, как обычный ответ.

Проверка:

- trigger click вызывает LLM только после клика;
- первый ответ использует stored context payload;
- `get_context()` не вызывается для prepared trigger path;
- при отсутствии/stale package включается явный fallback.

### 8. Добавить просмотр в админке

Файлы:

- `vchat/views/projects/views.py`
- `vchat/templates/projects/document_content.html`
- возможно шаблон страницы триггеров

Работа:

1. Показать на странице документа: package status, generated triggers,
   canonical page, selected chunks, fingerprint, дату сборки.
2. Показать причину отсутствия prepared package.
3. Добавить action пересборки package для страницы/источника, если потребуется.

Проверка:

- админ видит, почему у страницы есть или нет prepared triggers;
- action использует существующие CSRF/action patterns;
- ошибки сборки не скрываются.

## Ограничения

- Не отправлять DOM, HTML, `innerText`, значения форм или персонализированный
  frontend state как контекст.
- Не открывать публичному виджету доступ к глобальной базе знаний.
- Не делать офлайн-генерацию полноценных ответов дорогой моделью.
- Не добавлять второй механизм ignored params рядом с source rules.
- Не сохранять важное состояние только в `Page.meta`.
- Не вводить обязательный admin review перед использованием trigger package.
- Не трогать удаленные серверы без отдельного явного запроса.

## Проверка задачи

Автоматические проверки:

- `venv/bin/pytest tests/test_triggers.py -q`
- `venv/bin/pytest tests/chat/test_chat_views_stream.py -q`
- targeted tests для `widget_triggers_resolve`;
- targeted tests для URL normalization через source `param` rules;
- targeted tests для package builder;
- migration/model tests для новых таблиц;
- `venv/bin/ruff check ...` по измененным Python-файлам.

Поведенческие проверки:

- URL с ignored param использует тот же canonical URL, что и crawler.
- Временный параметр можно исключить через source config `param`.
- Unknown page через widget discovery попадает в очередь индексации.
- Duplicate page использует контекст основной страницы.
- Low-content page не получает page-specific triggers.
- Public trigger response содержит только `key` и `text`.
- LLM для ответа вызывается только после клика пользователя.
- Prepared trigger path не выполняет онлайн retrieval.

Проверки diff:

- нет `innerText`, `outerHTML`, DOM capture или отправки HTML страницы;
- нет расширения `allowed_source_ids` до глобального поиска для public widget;
- нет fallback-совместимости старого payload без явного решения.

## Открытые вопросы

1. Какие временные параметры уже нужно добавить в source rules для текущих
   источников: session id, access token, одноразовые campaign params?
2. Нужен ли отдельный action "пересобрать prepared triggers" сразу в первом
   этапе или достаточно фоновой задачи?
3. Храним ли prepared package только на canonical page или сразу допускаем
   cluster package для exact duplicates?
4. Какой дешевый provider/model выбрать значением по умолчанию для
   `trigger_generation_provider` и `trigger_generation_model`?
5. Какой минимальный объем контента считать достаточным для генерации
   page-specific triggers?

## Рекомендация

Двигаться в два этапа.

Первый этап: canonical page package без кластеров. Он закрывает главную дыру:
триггеры перестают быть строками без контекста, но runtime и клиентский API
остаются простыми.

Второй этап: кластеризация. Начать с exact duplicate и near-duplicate через
уже существующие hash/shingle механизмы. Более сложные смысловые кластеры
добавлять только после просмотра качества в админке и логах.
