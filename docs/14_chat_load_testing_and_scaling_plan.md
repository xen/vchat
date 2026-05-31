# План нагрузочного тестирования и масштабирования chat runtime

## Цель

Нужно ввести отдельный модуль нагрузочного тестирования для chat runtime и поэтапно подтвердить, что наша часть системы выдерживает `1000` одновременных chat-соединений.

Под "наша часть системы" в рамках этой задачи понимается:

- HTTP/WebSocket runtime на `aiohttp`;
- request lifecycle и middleware;
- Redis-интеграция;
- чтение и запись chat history в PostgreSQL;
- внутренние этапы подготовки контекста;
- фоновые постановки задач;
- локальный расчет векторов и связанные очереди;
- наблюдаемость и метрики capacity.

Внешний LLM provider не является критерием прохождения. Для проверки собственной емкости он должен быть отключаемым и заменяемым на детерминированный mock stream.

Задача делится на последовательные этапы:

1. `1000` одновременных пустых WebSocket-соединений без пользовательских сообщений.
2. `1000` одновременных соединений с mocked provider, который отдает поток случайных или циклических сообщений.
3. Те же соединения с реальным чтением history из БД и записью user/assistant messages в БД.
4. Те же соединения с расчетом векторов и полной внутренней post-processing цепочкой.

Документ нужен как постоянная точка возврата: он должен фиксировать этапы, критерии прохождения, ожидаемые узкие места, и список файлов, которые нужно менять по мере развития стенда.

## Current state

### Runtime и сеть

- Приложение поднимается через один `aiohttp` process в [entry.py](/Users/xen/Dev/sber/vchat/entry.py).
- Используется `uvloop`, если он доступен, но multiprocess server поверх `aiohttp.web.run_app(...)` сейчас не настроен.
- Основной chat transport — WebSocket route `GET /ws/chat/{payload}` в [vchat/routes.py](/Users/xen/Dev/sber/vchat/vchat/routes.py).
- Уведомления живут в отдельном `GET /ws/notify`, что добавляет второй long-lived WebSocket path в системе, но основной объект тестирования сейчас — `chat_ws`.

### WebSocket chat lifecycle

Обработчик [vchat/views/chat/views.py](/Users/xen/Dev/sber/vchat/vchat/views/chat/views.py) сейчас делает следующее:

1. Принимает WebSocket и готовит соединение.
2. Проверяет signed payload, извлекает `user_id` и `chat_id`.
3. Через отдельную DB session проверяет существование чата.
4. Помечает чат как активный через `redis.sadd("active_chats", ...)`.
5. На каждый входящий message:
   - прогоняет input guardrails;
   - публикует события в Redis monitor channel;
   - открывает отдельную DB session для `get_context(...)`;
   - стримит ответ через `ai_chat_stream(...)`;
   - прогоняет output guardrails;
   - открывает отдельную DB session для записи `ChatMsg`;
   - ставит background indexing tasks через `run_task(...)`.
6. На закрытии соединения вызывает `ws.close()` и `redis.srem("active_chats", ...)`.

Это означает, что даже один активный chat request уже затрагивает несколько внутренних систем: Redis, DB, provider stream и background queue.

### Database

- Async engine создается в [vchat/db.py](/Users/xen/Dev/sber/vchat/vchat/db.py).
- Текущие параметры пула:
  - `pool_size=5`
  - `max_overflow=10`
- Для request-scoped HTTP paths DB session создается в [vchat/middlewares/__init__.py](/Users/xen/Dev/sber/vchat/vchat/middlewares/__init__.py).
- Для WebSocket chat path используются отдельные `async_session_factory()` blocks внутри handler-а, а не request-scoped middleware session.

Это означает:

- idle WebSocket connections почти не должны нагружать БД после начальной проверки чата;
- но любое массовое одновременное сообщение быстро упрется в DB concurrency, если время удержания соединений окажется заметным;
- отдельные DB phases можно тестировать изолированно.

### Redis

- Redis connection создается в [vchat/app.py](/Users/xen/Dev/sber/vchat/vchat/app.py) и кладется в app state.
- Chat runtime использует Redis и напрямую, и через shared helper code:
  - `active_chats`;
  - `publish` в `chat_monitor:*`;
  - queueing background tasks;
  - вспомогательные процессы embeddings/celery.

Следствие: на ранних этапах тестов Redis должен измеряться отдельно, даже если provider замокан.

### Canonical source of truth

- Каноническое состояние chat history хранится в таблицах `Chat` и `ChatMsg`; runtime работает с ними через модели в [vchat/models/data.py](/Users/xen/Dev/sber/vchat/vchat/models/data.py).
- Redis в этом контуре — derived runtime infrastructure: presence, fan-out, transient queueing, notifications.
- Векторные индексы и embeddings — derived artifacts, которые должны тестироваться отдельно после базового DB/runtime этапа.

### Ограничения, которые уже есть в коде

1. Один `aiohttp` process.
2. Один event loop на process.
3. Небольшой DB pool по умолчанию.
4. Несколько внутренних async phases на один chat message.
5. Background queue включается прямо из chat runtime.
6. Provider stream и локальные этапы пока не разделены через явную test-mode abstraction.

## Relevant files

- [entry.py](/Users/xen/Dev/sber/vchat/entry.py)
- [vchat/app.py](/Users/xen/Dev/sber/vchat/vchat/app.py)
- [vchat/routes.py](/Users/xen/Dev/sber/vchat/vchat/routes.py)
- [vchat/db.py](/Users/xen/Dev/sber/vchat/vchat/db.py)
- [vchat/middlewares/__init__.py](/Users/xen/Dev/sber/vchat/vchat/middlewares/__init__.py)
- [vchat/views/chat/views.py](/Users/xen/Dev/sber/vchat/vchat/views/chat/views.py)
- [vchat/views/chat/ctx.py](/Users/xen/Dev/sber/vchat/vchat/views/chat/ctx.py)
- [vchat/models/data.py](/Users/xen/Dev/sber/vchat/vchat/models/data.py)
- [vchat/metrics.py](/Users/xen/Dev/sber/vchat/vchat/metrics.py)
- [vchat/config.yaml](/Users/xen/Dev/sber/vchat/vchat/config.yaml)
- [jobs/celery.py](/Users/xen/Dev/sber/vchat/jobs/celery.py)
- [jobs/embedder/tasks.py](/Users/xen/Dev/sber/vchat/jobs/embedder/tasks.py)

Новые файлы, которые, скорее всего, понадобятся:

- `vchat/loadtest/` — отдельный модуль для генераторов нагрузки, сценариев и утилит.
- `vchat/loadtest/README.md` или отдельная документация по запуску.
- `tests/load/` или `scripts/load/` для smoke/CI сценариев, если нужен минимальный прогон.
- `vchat/loadtest/providers.py` или аналогичный модуль для mocked provider behavior.
- `vchat/loadtest/metrics.py` или интеграция с существующим `vchat/metrics.py`.

## Models and data

### Канонические данные

Канонические сущности для chat runtime уже существуют:

- `Chat`
- `ChatMsg`

Для первых этапов нагрузочного тестирования schema changes в этих моделях не обязательны.

### Производные данные и runtime artifacts

Для самой системы нагрузочного тестирования понадобятся не доменные модели, а runtime/test artifacts:

- profile test run;
- sampled latency metrics;
- concurrency snapshots;
- error aggregates;
- resource usage snapshots.

На старте их не нужно хранить в PostgreSQL. Правильнее сначала писать результаты в:

- structured logs;
- Prometheus-compatible metrics;
- файловые отчеты в `data/` или отдельную `artifacts/loadtest/`.

Если позже понадобится история прогонов, можно ввести отдельную модель `LoadTestRun`, но сейчас это premature.

### Что нужно сделать с конфигурацией

Для тестового контура нужны явные runtime flags, а не ad-hoc monkeypatch:

- `chat_provider_mode = real | mock`
- `chat_mock_stream_mode = cyclic | random`
- `chat_mock_stream_chunk_count`
- `chat_mock_stream_chunk_delay_ms`
- `chat_runtime_disable_embeddings = true | false`
- `chat_runtime_disable_monitor_publish = true | false`
- `chat_runtime_disable_guardrails = true | false`
- `chat_runtime_disable_db_writes = true | false`
- `chat_runtime_disable_context_read = true | false`

Эти флаги не меняют каноническую бизнес-модель, но нужны как controlled switches для поэтапной изоляции bottleneck-ов.

### Правила инвалидации и производных артефактов

Для этого контура важно различать:

- business data, которая должна остаться корректной после теста;
- test data, которую можно чистить целиком.

Поэтому для DB этапов нужно заранее определить:

- отдельные test users / test chats;
- отдельный cleanup policy после прогона;
- фиксированный префикс или метка для generated test messages;
- правила очистки embeddings/index tasks, если они ставятся во время теста.

## Implementation plan

### 1. Базовая структура load testing module

Нужно создать отдельный модуль, например `vchat/loadtest/`, с четким разделением:

1. Генерация соединений.
2. Описание сценариев.
3. Сбор client-side метрик.
4. Агрегация результатов.
5. CLI entrypoint для запуска конкретного профиля.

Рекомендуемая структура:

```text
vchat/loadtest/
  __init__.py
  cli.py
  profiles.py
  websocket_client.py
  scenarios.py
  reporters.py
  config.py
  providers.py
  db.py
  vectors.py
```

Смысл:

- `cli.py` — запуск конкретного профиля;
- `profiles.py` — готовые сценарии вроде `idle_1000`, `mock_stream_1000`;
- `websocket_client.py` — низкоуровневая работа с WS;
- `scenarios.py` — orchestration connect/send/receive/close;
- `reporters.py` — stdout/json summary;
- `providers.py` — mocked provider behavior;
- `db.py` и `vectors.py` — более поздние фазы, когда тест дойдет до БД и embeddings.

### 2. Этап 1: `1000` idle WebSocket connections

Цель:

- держать `1000` одновременных подключений к `GET /ws/chat/{payload}`;
- не отправлять пользовательские chat messages;
- не ломать стандартное поведение браузера с ping/pong.

Что именно проверяем:

- открываются ли все `1000` соединений;
- сохраняются ли они стабильными заданное время;
- сколько памяти и CPU потребляет один `aiohttp` process;
- нет ли роста file descriptors;
- нет ли утечек Redis presence state;
- как ведет себя event loop при длительном удержании.

Минимальный профиль:

- ramp-up: `50-100` соединений в секунду;
- target: `1000` открытых WS;
- hold duration: `10-15` минут;
- без user messages;
- с ping/pong согласно поведению браузера или используемой WS client library.

Критерии прохождения этапа:

- `1000/1000` соединений успешно открыты;
- не более `0.5%` неожиданных disconnect/error за hold interval;
- нет постоянного роста RSS;
- нет роста `active_chats`, который переживает завершение теста;
- нет saturation по CPU на idle-сценарии.

Ожидаемые узкие места этапа 1:

- `ulimit -n`;
- reverse proxy connection limits, если тест идет через proxy;
- memory footprint одного процесса;
- Redis presence bookkeeping;
- event loop stalls;
- file descriptor leaks.

### 3. Этап 2: mocked provider stream

После подтверждения idle capacity нужно оставить те же `1000` WS, но включить controlled activity.

Цель:

- проверить собственный streaming path без реального внешнего provider;
- отделить узкие места runtime от vendor latency/rate limits.

Что должен делать mock provider:

- отдавать deterministic stream chunks;
- поддерживать режим `cyclic` и `random`;
- уметь имитировать:
  - короткий ответ;
  - длинный ответ;
  - много маленьких чанков;
  - few large chunks;
  - controlled delay между чанками.

Нужно не monkeypatch в тесте, а явный runtime adapter внутри chat layer, чтобы можно было запускать нагрузку повторяемо и без ручного редактирования кода.

Критерии прохождения этапа:

- `1000` одновременных соединений выдерживают активный stream;
- нет cascade disconnect;
- latency на first chunk и completion предсказуема;
- CPU и memory масштабируются линейно или близко к линейному профилю;
- Redis publish/monitor path не становится точкой отказа сам по себе.

Ожидаемые bottleneck-и:

- JSON serialization на большом числе partial messages;
- backpressure при send_json;
- Redis publish volume;
- накопление `total_content` и in-memory `messages`;
- один process на весь runtime.

### 4. Этап 3: реальное чтение и запись в БД

После mock-only этапа включаем:

- чтение history/context из БД;
- запись `ChatMsg` в БД;
- при необходимости запись только части payload, чтобы отдельно замерить insert/read cost.

Цель:

- понять предельную concurrency нашей DB-схемы и текущего session lifecycle;
- увидеть реальное время checkout/checkin;
- найти места, где DB connection удерживается слишком долго.

Что должно появиться перед этим этапом:

- instrumented DB pool metrics;
- checkout/checkin counters;
- connection wait time;
- per-phase timers:
  - auth/check phase;
  - context read phase;
  - message persist phase.

Критерии прохождения этапа:

- нет `QueuePool limit ... reached` на целевом сценарии;
- DB pool pressure наблюдаема и объяснима;
- p95/p99 write latency не уходит в неконтролируемый рост;
- cleanup test messages выполняется безопасно и воспроизводимо.

Ожидаемые bottleneck-и:

- слишком маленький pool;
- длинные context queries;
- burst inserts;
- contention на индексы;
- накопление unconsumed background tasks.

### 5. Этап 4: расчет векторов и полная post-processing цепочка

После подтверждения WS + mock + DB включается полный внутренний контур:

- контекст;
- запись history;
- постановка задач;
- embeddings/index updates;
- любые дополнительные derived pipelines.

Цель:

- измерить полную internal throughput capacity без зависимости от внешнего provider;
- увидеть, какой слой деградирует первым: app, DB, Redis, Celery, embedder workers или storage.

Критерии прохождения этапа:

- нет бесконтрольного роста очередей;
- обработка embeddings не уходит в вечный backlog;
- chat runtime не блокируется из-за фоновых этапов;
- есть понятный ceiling и понятная scaling strategy.

Ожидаемые bottleneck-и:

- CPU на embeddings;
- worker count;
- Redis broker pressure;
- DB contention на индексируемых таблицах;
- memory pressure от очередей и batch processing.

### 6. Метрики и observability

До начала практических прогонов нужно добавить или гарантировать сбор следующих метрик.

Обязательные runtime metrics:

- текущее число открытых chat WS;
- connect success/error count;
- disconnect count с причинами;
- p50/p95/p99 connection duration;
- p50/p95/p99 time to first chunk;
- p50/p95/p99 full response duration;
- sent/received bytes per connection;
- partial message count per response.

Обязательные server metrics:

- process RSS;
- CPU user/system;
- event loop lag;
- open file descriptors;
- socket counts;
- GC activity, если понадобится.

Обязательные DB metrics:

- active connections;
- pool checkout/checkin counts;
- pool wait time;
- query time by phase;
- error rate on context read / insert.

Обязательные Redis metrics:

- connected clients;
- publish latency;
- ops/sec;
- queue depth;
- `active_chats` cardinality.

Обязательные background metrics:

- queue depth по embeddings задачам;
- worker concurrency;
- job processing latency;
- failure/retry counts.

### 7. Масштабирование и ожидаемый scaling behavior

На старте нужно считать, что система масштабируется по-разному на разных этапах:

- idle WS connections должны масштабироваться почти линейно по памяти и fd;
- mock stream stage будет ограничен одним `aiohttp` process и send/serialize overhead;
- DB stage упрется в пул и query time;
- vector stage упрется в worker CPU и очередь.

Вероятные следующие меры масштабирования:

1. Multiprocess runtime для `aiohttp`.
2. Явное разделение chat runtime и background-heavy flows.
3. Отдельные Redis роли или хотя бы отдельные db/instance для presence и broker.
4. Перенос тяжелых post-processing этапов из request-adjacent path.
5. Пересмотр DB pool sizing и query shape.
6. Явные admission control / backpressure rules для active chat generation.

### 8. Проверка "прошли / не прошли"

Для каждого этапа нужен не только график, но и бинарный verdict:

- `PASS` — целевой concurrency достигнут и удержан;
- `SOFT FAIL` — система работает, но нарушены SLO или наблюдается неконтролируемый ресурсный рост;
- `HARD FAIL` — массовые disconnect/errors или системная деградация.

Для этапа 1 verdict должен быть основан на:

- числе стабильно удержанных соединений;
- стабильности памяти;
- стабильности CPU;
- отсутствии orphan state в Redis;
- отсутствии network/runtime errors сверх допустимого порога.

## Summary and recommendation

Правильный порядок работы такой:

1. Сначала сделать отдельный `vchat/loadtest` модуль.
2. Сначала реализовать только idle WS profile.
3. Добавить детальную observability до перехода к mock stream.
4. Затем ввести provider abstraction и mocked stream.
5. Только после этого включать реальные DB read/write.
6. И только потом включать embeddings/indexing.

Главная рекомендация: не пытаться сразу мерить "1000 активных чатов" end-to-end. Для engineering capacity вам нужен изолируемый стенд, где каждый следующий этап включает только один новый bottleneck.

Главный риск текущего состояния — смешение нескольких нагрузок в одном runtime path:

- WebSocket lifetime;
- Redis presence;
- DB read/write;
- provider stream;
- post-processing;
- background queueing.

Если не разнести это на этапы и флаги, любой график будет шумным и плохо объяснимым.

## Open questions

1. Нужен ли отдельный test-only endpoint для генерации подписанного `chat_ws` payload, или тестовый модуль будет использовать существующий signer внутри Python runtime?
2. Запускать нагрузку из того же процесса/репозитория или из отдельного внешнего runner host?
3. Хотим ли сразу ориентироваться на один process capacity или на итоговую capacity всей deployment topology?
4. Нужно ли сохранять историю прогонов в БД, или на первом этапе достаточно JSON/log artifacts?
5. Должен ли idle stage идти через реальный reverse proxy, или сначала мерим чистый backend port?

## Follow-up work

1. Реализовать `vchat/loadtest/cli.py` и профиль `idle_1000`.
2. Добавить server-side gauges для open chat websockets.
3. Добавить DB pool instrumentation.
4. Добавить mock provider adapter.
5. Добавить cleanup tooling для test chats/messages.
6. Добавить profiling playbook: что смотреть в `top`, `lsof`, Redis, Postgres и Prometheus во время прогона.

## Первый запуск

Текущий scaffold уже содержит CLI для первого этапа:

```bash
venv/bin/python -m vchat.loadtest.cli idle-ws \
  --base-url http://127.0.0.1:9080 \
  --user-id 1 \
  --chat-id YOUR_EXISTING_CHAT_ID \
  --target-concurrency 1000 \
  --ramp-per-second 100 \
  --hold-seconds 600 \
  --report-json-path data/loadtest/idle-ws-report.json
```

Замечания:

- `chat_id` должен существовать в БД, потому что `chat_ws` проверяет его через `Chat.id`.
- Вместо `--user-id` и `--chat-id` можно передать готовый `--payload`.
- Для локального self-signed TLS можно добавить `--insecure`.
- Это пока только этап `idle-ws`: соединения открываются, держатся и закрываются без пользовательских chat messages.
