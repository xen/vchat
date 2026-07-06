# Матрица негативных сценариев и реакции системы

Документ фиксирует ожидаемое поведение `vchat` в негативных сценариях и
проверки, которыми это поведение подтверждается. Цель - заранее отделить
штатную отказоустойчивость от ошибок, которые должны падать громко и
диагностироваться по traceback, метрикам и состоянию очередей.

## Общие принципы реакции

- Сбои инфраструктуры не маскируются: БД, Redis, Celery, LLM-провайдер,
  embedder и crawler должны давать явную ошибку в логах, а не тихий fallback.
- Пользовательские ошибки ввода возвращают понятный отказ без изменения данных.
- Публичные входы работают по принципу "запрещено по умолчанию": виджет не
  получает доступ к проектным данным без явной привязки и включенного состояния.
- Фоновые задачи не блокируют aiohttp request/websocket event loop.
- Операции изменения состояния защищены авторизацией и CSRF/подписанными
  параметрами там, где это предусмотрено интерфейсом.
- Проверка сценария должна включать не только HTTP-ответ, но и состояние БД,
  очереди, метрики или пользовательский след, если сценарий меняет данные.

## Матрица сценариев

| Зона | Негативный сценарий | Ожидаемая реакция | Как проверяется |
| --- | --- | --- | --- |
| Auth | Неавторизованный пользователь открывает закрытую страницу проекта | Редирект на страницу входа; данные проекта не раскрываются | Unit/smoke-тест защищенных route-ов; проверка, что ответ ведет на `/login/` и не содержит проектных данных |
| Auth | Пользователь без прав пытается открыть чужой проект, историю чата или документ | HTTP 403/404 по принятому договору доступа; данные чужого проекта не попадают в HTML/JSON | Тест route-а с пользователем без доступа; проверка отсутствия чужих `project_id`, `chat_id`, source/document content в ответе |
| Auth | POST-действие в проекте отправлено без CSRF или с испорченной подписью | Действие отклоняется; состояние БД не меняется | Тесты POST-действий без CSRF и с неверной подписью; проверка отсутствия commit/side effects |
| Sessions | Пользователь отзывает текущую или все сессии | Отозванные сессии перестают авторизовывать запросы; активная разрешенная сессия остается только если это явно предусмотрено действием | Тест действия отзыва; проверка таблицы/хранилища сессий и следующего запроса с отозванной cookie |
| Admin | Неадминистратор открывает административные страницы или выполняет admin action | Доступ запрещен; событие не выполняется | Тест с обычным пользователем; проверка HTTP-статуса и отсутствия записи/изменения admin-сущностей |
| Config | Production запущен с дефолтным или placeholder `secret_key`/`cookie_key` | Приложение падает на старте с явной ошибкой конфигурации | Unit-тест загрузки настроек production; проверка текста ошибки и отсутствия успешного app init |
| Config | Env override имеет неверный тип или имя, не соответствующее плоским lowercase-ключам | Конфигурация отклоняется или нормализуется по существующему договору; новые aliases не появляются | Unit-тест `vchat.settings`; проверка значения в `cfg` и ошибки для неверного типа |
| Public widget | Запрошен несуществующий, удаленный или выключенный widget code | Виджет/чат не раскрывает проектные данные; состояние в cache помечает missing/disabled, если используется cache | Тест `/widget/{code}` и `/chat/widget/{code}`; проверка отсутствия источников и корректного widget state |
| Public widget | Widget code сброшен админом, старый код продолжает использоваться на сайте партнера | Старый код перестает работать; новый код работает только в своем текущем состоянии | Тест `widget_reset_code`; проверка cache для old/new code и ответа публичного endpoint-а |
| Public widget | Secret API-клиента/виджета сброшен, старый secret используется для интеграции | Запрос со старым secret отклоняется; новый secret принимает только корректно подписанный запрос | Тест reset secret и публичного API; проверка отказа старого секрета и отсутствия частичного update |
| Public widget | Widget не имеет явной привязки к разрешенным источникам | Retrieval не ищет по всему проекту; отсутствие привязки трактуется как ошибка конфигурации | Тест retrieval context для widget; проверка списка source/page ids в запросе к retrieval |
| Widget content | В настройках виджета сохранены HTML/скрипты в приветствии, footer или ошибке | Разрешенный HTML сохраняется по договору, опасные теги/атрибуты удаляются; скрипт не исполняется | Тест формы widget edit; браузерная проверка DOM для сохраненных сообщений, отсутствие `<script>` и опасных handlers |
| Chat | Websocket payload поврежден, просрочен или относится к чужому проекту | Соединение отклоняется без генерации ответа и без раскрытия истории | Unit/integration-тест websocket handshake; проверка отсутствия новых `chat_msg` |
| Chat | LLM-провайдер недоступен или возвращает ошибку | Ошибка видна пользователю через настроенное сообщение чата/виджета; в логах есть traceback/контекст; fallback на другой provider не включается сам | Тест с провайдером, бросающим исключение; проверка ответа клиенту и логов |
| Chat | Ответ LLM пытается сослаться на источник, которого нет в retrieval context | Источник не показывается как подтвержденный; ответ остается без ложной source-card/citation | Тест построения source cards; проверка, что карточки берутся только из retrieval context |
| Chat | Пользователь просит данные вне разрешенной базы знаний виджета | Retrieval ограничен widget/source scope; модель получает guardrails/system prompt с границами доступа | RAG-тест с чужим source; проверка prompt/context и отсутствия чужого chunk content |
| Chat | Сообщение содержит prompt injection из документа или пользователя | Инструкция из документа не должна менять системные правила, источники и границы доступа | RAG/guardrails-тест с инъекцией в chunk content; проверка финального prompt и ответа |
| RAG | В базе нет релевантных chunk-ов или все chunk-и исключены | Ответ сообщает, что данных недостаточно; система не выдумывает факты и не ищет вне scope | Retrieval fixture/eval; проверка пустого context и текста ответа без неподтвержденных фактов |
| RAG | Chunk-и дублируются или документ переиндексирован без изменения содержания | Повторная индексация не размножает одинаковые chunk-и; near-duplicate logic сохраняет целостность | Тест indexing/documents; сравнение количества chunk-ов до/после повторной обработки |
| RAG | Embedding отсутствует у части chunk-ов | Retrieval не должен молча считать данные полноценно готовыми; embedder queue/метрика показывает backlog | Тест выборки retrieval с `embedding is null`; проверка счетчика backlog и очереди embedder |
| Crawler | Source заблокирован правилами источника или политикой URL | Обход не ставится в очередь; пользователю показывается отказ/статус блокировки | Тест `crawl_source` для blocked source; проверка, что `crawl_page_task` не вызван |
| Crawler | URL не `http/https`, локальный файл, внутренний service URL или malformed URL | URL отклоняется до скачивания; нет сетевого запроса и записи небезопасного source/page | Unit-тест валидатора URL/source config; проверка отсутствия новых записей |
| Crawler | Удаленный ответ слишком большой | Streaming останавливается на лимите; парсер не получает полный oversized body | Unit/integration-тест streaming limit; проверка исключения и отсутствия сохраненного raw oversized content |
| Crawler | Удаленный сайт отвечает timeout/5xx/redirect loop | Page получает диагностируемый статус ошибки; задача завершается без бесконечных retries | Тест crawler task с fake HTTP client; проверка `PageStatusError`, логов и количества retry |
| Crawler | Sitemap содержит слишком много URL или запрещенные URL | Обрабатываются только разрешенные URL в пределах политики; запрещенные URL не попадают в очередь | Тест sitemap discovery; проверка списка поставленных задач и примененных source rules |
| Documents | Пользователь загружает документ неподдерживаемого типа или пустой файл | Загрузка отклоняется без создания пригодного для retrieval документа | Тест формы/route загрузки; проверка flash/ошибки и отсутствия chunk-ов |
| Documents | CSV export содержит значения, похожие на формулы | Export нейтрализует formula injection | Тест CSV export; проверка, что значения начинаются с безопасного префикса |
| Documents | Пользователь пытается обновить страницу, не принадлежащую source | Refresh отклоняется; фоновая задача не ставится | Тест `refresh_page` для документа без `source_id`/`uri`; проверка отсутствия вызова `crawl_page_task` |
| Embedder | Celery broker Redis недоступен при постановке задачи | Запрос/операция падает явно; задача не считается поставленной | Unit/integration-тест с недоступным broker mock; проверка исключения и отсутствия success flash |
| Embedder | Embedder worker остановлен, очередь растет | Web остается живым, но readiness/метрики/операционный чек показывают backlog; данные не считаются проиндексированными | Проверка `/metrics`, очереди broker Redis, количества `chunk.embedding is null`; в локальном тесте - collector test |
| Embedder | Настроено недоступное устройство `mps`/`cuda` | Worker падает на старте с явной ошибкой устройства | Unit-тест device resolver; проверка исключения для недоступного backend-а |
| Celery | Новая task не зарегистрирована или переименована без миграции вызовов | Тест регистрации падает; старые deprecated task names не добавляются без явного решения | `venv/bin/pytest tests/test_celery_task_registration.py`; ревизия вызовов `.delay()` |
| Triggers | Генерация triggers запущена без CSRF/подписанного действия | Генерация не ставится в очередь | Тест `project_triggers_requires_signed_csrf`; проверка отсутствия вызова Celery task |
| Triggers | Нет подходящих документов/страниц для генерации trigger rules | Задача завершается без мусорных triggers; интерфейс показывает пустое/нейтральное состояние | Тест `jobs/triggers`; проверка количества созданных rules и UI count |
| Integration API | `/api/update` получает неверный client id/secret или подпись | HTTP 401/403; документ не создается и не обновляется | API-тест; проверка БД до/после и audit/admin event |
| Integration API | Интеграционный update ссылается на project/source вне прав клиента | Запрос отклоняется; cross-project update невозможен | API-тест с клиентом другого проекта; проверка отсутствия записи/изменения target page |
| Integration API | Повторный update приходит с тем же содержанием | Система не создает лишнюю переиндексацию, если content effectively unchanged | Тест document indexing; проверка отсутствия лишних chunk/index jobs |
| Metrics | Prometheus gauge очереди читает Redis приложения вместо Celery broker | Метрика показывает неверный backlog; это считается ошибкой проверки | Unit-тест collector-а с broker URL; локально сверить DB index из `cfg.celery_broker_url` |
| Health | `/health/live` доступен, но `/health/ready` должен отражать неготовность зависимостей | Liveness не перезапускает процесс без причины; readiness снимает инстанс с трафика при критичной неготовности | Тест health handlers; ручная локальная проверка с отключенной зависимостью, если сценарий реализован |
| Static | Static routes следуют symlink-ам или отдают файлы вне dist/static дерева | Маршрут не должен следовать symlink; приватные файлы не отдаются | `venv/bin/pytest tests/test_routes_security.py` |
| Error pages | 403/404/405/500 возвращают сломанный шаблон или раскрывают traceback пользователю | Пользователь видит корректную error page; traceback остается в логах | `venv/bin/pytest tests/test_error_templates.py`; ручная проверка HTML при локальном запуске |
| UI | HTMX/action response вернул неверный partial или не послал ожидаемый `HX-Trigger` | Интерфейс не обновляет состояние; это должно ловиться route-тестом | Тест конкретного action response; проверка статуса, headers, body partial |
| UI | Локальная видимая поломка layout/DOM на странице проекта | Исправление делается после просмотра реального DOM в Codex browser; соседняя страница того же паттерна проверяется тоже | Браузерная проверка локального URL во встроенном браузере Codex; screenshot/DOM before-after |
| DB migration | Миграция меняет таблицы с данными без сверки количества записей | Миграция не принимается без проверки целостности до/после | Перед/после: count релевантных таблиц, выборочные invariants, `venv/bin/pytest tests/test_migration_schema.py` |
| DB model | JSONB `Page.meta` меняется in-place и SQLAlchemy не видит изменение | Изменение не сохраняется; правильная реакция - использовать copy + reassignment или методы модели | Unit-тест модели/meta; проверка SQLAlchemy dirty state/commit result |
| Logging | Ошибка фоновой задачи или request теряет контекст project/page/source/chat | Инцидент трудно расследовать; лог должен содержать идентификаторы сущностей и traceback | Тест логирования при exception path или ручная проверка structured log на локальном сценарии |
| Audit | Изменение секретов, widget code, пользователей, источников или API clients не оставляет admin event | Операция считается неполной: должен быть audit/admin event | Unit-тест action; проверка вызова `admin_event` и содержимого события |
| Privacy | История чата/экспорт/виджет показывает внутренние ids, secrets или чужие данные | Ответ/HTML/CSV не должен содержать секреты и данные вне доступа | Тест сериализации/шаблона; grep по response body на `secret`, чужие ids, raw tokens |

## Минимальный набор регрессионных проверок перед приемкой

Запускать из проектного virtualenv:

```bash
venv/bin/pytest \
  tests/test_utils_and_settings.py \
  tests/test_routes_security.py \
  tests/test_error_templates.py \
  tests/test_projects_actions_extended.py \
  tests/test_api_views.py \
  tests/test_celery_task_registration.py \
  tests/test_migration_schema.py \
  tests/test_logged_in_site_smoke.py
```

Для изменений RAG/chat дополнительно:

```bash
venv/bin/pytest \
  tests/test_chat_retrieval_ctx.py \
  tests/test_retrieval_ctx.py \
  tests/chat/test_guardrails_websocket.py \
  tests/chat/test_system_prompt_policy.py \
  tests/rag_quality/test_retrieval_fixture_eval.py \
  tests/rag_quality/test_answer_grounding_eval.py
```

Для crawler/indexing/embedder:

```bash
venv/bin/pytest \
  tests/test_crawler_tasks.py \
  tests/test_crawler_overhaul.py \
  tests/test_source_blocking.py \
  tests/test_source_settings_and_pause.py \
  tests/test_document_pipeline.py \
  tests/test_document_indexing.py \
  tests/test_embedder_chunking_limits.py \
  tests/test_embedder_parallelism.py
```

## Что фиксировать при проверке инцидента

- Точный URL/route, пользователь, проект, chat id, source id, page id или widget
  code, если они есть в сценарии.
- HTTP-статус, redirect target, `HX-Trigger`, websocket close/error payload или
  API JSON.
- До/после: записи БД, количество chunk-ов, `embedding is null`, состояние
  source/page status, audit/admin events.
- Состояние очередей Celery broker Redis и соответствующие Prometheus-метрики,
  если сценарий связан с background jobs.
- Traceback и structured log с идентификаторами сущностей.
- Для UI - реальное состояние DOM/страницы во встроенном браузере Codex, а не
  только `curl` или предположение по шаблону.
