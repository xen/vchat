# Task: Ограничить параллельный embedding encode в web request path

## Goal

Web backend должен выполнять локальный embedding encode из request path через
единый ограниченный механизм, который не запускает неограниченное число
одновременных `SentenceTransformer.encode()` при всплеске запросов и сохраняет
текущий контракт сохранения пользовательских сообщений, RAG-контекста и
guardrail-веток.

## Context

- `vchat/views/chat/ctx.py`: `embed_query()`, `_get_embed_model()`,
  `get_context()`.
- `vchat/views/chat/views.py`: request-path сохранение `user_embedding` в
  cached trigger response и guardrail-ветках.
- `jobs/embedder/model.py`: загрузка `SentenceTransformer`, выбор устройства и
  release torch cache.
- `jobs/embedder/launcher.py`: существующий пример process-based CPU scaling
  для document embedder. Это источник ограничений, а не целевой путь переноса
  request embeddings в отдельную очередь.
- `vchat/config.yaml`: embedding-настройки и текущая плоская конфигурация.
- `tests/chat/test_ctx_module.py`: unit-тесты chat context и embedding query.
- `tests/chat/test_guardrails_websocket.py`,
  `tests/chat/test_live_llm_scenarios.py`: проверки websocket/request-path
  сценариев, где `embed_query` сейчас monkeypatch-ится.
- `tests/test_embedder_parallelism.py`: проверки, что CPU auto-scaling идет
  количеством embedder instances.

## Current Behavior

- `embed_query()` синхронно вызывает локальную embedding-модель.
- Часть request path вызывает `embed_query()` через `asyncio.to_thread()`, чтобы
  не блокировать event loop.
- `asyncio.to_thread()` использует default executor процесса и не задает
  доменный лимит конкурентного embedding encode.
- При нескольких одновременных запросах один web process может одновременно
  выполнять несколько `model.encode()` на одной тяжелой локальной модели.
- Для document embedder уже закреплена другая модель capacity: внутри worker
  concurrency равен `1`, а CPU масштабируется несколькими отдельными worker
  processes. На сервере нельзя рассчитывать на MPS/GPU; несколько параллельных
  `encode()` в одном backend process могут не дать throughput, но увеличить
  память, риск падения процесса и tail latency.
- Перенос request embeddings в общий фоновой worker/service не является
  очевидным улучшением: такой worker может быть слабее backend hosts, добавить
  сетевую/очередную задержку и создать общий bottleneck с большой очередью.

## Target Shape

- Задача сначала дает измеренную модель запуска, а не только кодовый wrapper:
  сколько backend worker processes допустимо на CPU-only host, сколько памяти
  нужно на загруженную модель, как ведет себя локальная очередь ожидания и где
  начинается деградация.
- В коде есть единая async-функция для request-path query embedding, например
  `embed_query_async(text)`.
- Все request-path места вызывают только эту async-функцию, а не прямой
  `asyncio.to_thread(embed_query, ...)`.
- Внутри async-функции есть доменный limiter и dedicated executor с
  `max_workers=1`. Это не оптимизация throughput, а предохранитель: один backend
  process не должен запускать несколько crunching embedding одновременно.
- Лимит для web process по умолчанию равен `1`. Увеличение выше `1` считается
  отдельным capacity-решением и требует CPU-only server-like benchmark.
- Внутри process есть явная небольшая очередь ожидания. Пиковые пользователи
  могут ждать ограниченное время, ориентир для исследования: `20-30` секунд.
- После исчерпания локального лимита ожидания request получает явный отказ или
  контролируемое завершение, а не создает еще один encode и не зависает
  бесконечно.
- Ожидание limiter-а и отказ по переполнению наблюдаемы через лог или метрику,
  чтобы увидеть очередь embedding encode.
- Ошибки модели не проглатываются и проходят fail-fast с traceback.
- Для горизонтального capacity используются несколько backend instances /
  worker processes, согласованные с числом физических CPU cores и памятью, а не
  неограниченный thread pool внутри одного процесса.
- Балансировка нагрузки между backend worker processes должна быть частью
  проверки: нельзя получить ситуацию, где очередь одного process забита, а
  остальные простаивают, без понимания поведения nginx/gunicorn/aiohttp runtime.
- Отдельный request-embedding worker/service рассматривается только как
  исследуемая альтернатива на тестовом стенде, а не как предполагаемый следующий
  шаг.

## Guard Rails

- Не переносить всю RAG-архитектуру в Celery в этой задаче без отдельного
  подтверждения.
- Не переносить request embeddings в текущую document embedder queue без
  доказательства, что это быстрее и безопаснее backend worker processes.
- Не увеличивать concurrent `SentenceTransformer.encode()` внутри одного web
  process выше `1` без отдельного server-like benchmark.
- Не рассчитывать на MPS/GPU для серверного решения.
- Не добавлять fallback на пустой embedding, пропуск RAG или silent degraded
  mode.
- Не добавлять lazy import или `try/except ModuleNotFoundError` вокруг обычных
  зависимостей.
- Не менять shape `ChatMsg.embedding`, `used_chunks`, `ContextResult` и
  публичные websocket/API payloads.
- Не обращаться к тестовому серверу без отдельного разрешения.
- Не менять worker/embedder jobs для document chunks, если изменение не нужно
  для общего helper-а загрузки модели.

## Iterations

1. Инвентаризировать request-path embedding calls и текущие capacity tests.
   Контрольная точка: все прямые `asyncio.to_thread(embed_query, ...)` в
   `vchat/views/chat/*` перечислены и понятно, какие из них должны перейти на
   общий bounded helper; `tests/test_embedder_parallelism.py` учтен как
   существующий baseline process-based scaling.

2. Сделать исследовательский стенд для embedding runtime.
   Контрольная точка: есть локальный benchmark/test harness, который запускает
   один и тот же `embed_query` в режимах:
   direct sync внутри одного process, `ThreadPoolExecutor(max_workers=1)`,
   `ThreadPoolExecutor(max_workers>1)` только как негативный/сравнительный
   сценарий, несколько backend-like processes. Для каждого режима фиксируются
   latency p50/p95, RSS, max active encode, ошибки и признаки нестабильности.

3. Проверить thread/process safety модели.
   Контрольная точка: стенд доказывает, что вытеснение encode в dedicated
   thread не приводит к segfault, зависанию event loop или неконтролируемому
   росту памяти на серии повторных вызовов. Если доказательства нет, решение
   должно остаться process-only или быть вынесено в отдельную задачу.

4. Ввести bounded async helper.
   Контрольная точка: в `vchat/views/chat/ctx.py` есть одна async-точка входа
   для query embedding; она использует `Semaphore` и dedicated executor
   `max_workers=1`, типизированные настройки и не меняет синхронный
   `embed_query()` как низкоуровневую функцию.

5. Добавить локальную очередь и backpressure.
   Контрольная точка: helper ограничивает не только active encode, но и ожидание
   в очереди: есть настраиваемый timeout/queue policy, ориентир `20-30` секунд
   для пиков. При превышении лимита система отвечает явно, без silent fallback и
   без запуска дополнительного encode.

6. Перевести request path на bounded helper.
   Контрольная точка: `get_context()` и сохранение `user_embedding` в
   `vchat/views/chat/views.py` больше не вызывают `asyncio.to_thread()` для
   `embed_query` напрямую.

7. Добавить регрессионные тесты конкурентности и очереди.
   Контрольная точка: тест запускает несколько одновременных вызовов helper-а и
   доказывает, что фактический parallel encode внутри одного process равен `1`;
   отдельный тест доказывает поведение при переполнении ожидания.

8. Добавить тесты сохранения контрактов request path.
   Контрольная точка: существующие websocket/guardrail тесты проходят, а новые
   или обновленные тесты проверяют, что `user_embedding` все еще передается в
   `persist_guardrail_messages` и `ChatMsg` insert.

9. Проверить поведение под локальным стрессом.
   Контрольная точка: небольшой локальный async-test или focused integration
   check показывает, что 10 конкурентных запросов не запускают 10 encode
   одновременно; лишние вызовы ждут limiter, а event loop продолжает отвечать
   на легкие операции.

10. Проверить балансировку между backend worker processes.
    Контрольная точка: на тестовом стенде или локальной multi-process модели
    видно, как распределяются 10-50 одновременных запросов: не возникает
    систематического перекоса, где один process копит очередь, а остальные
    пустые.

11. Сформировать реалистичный runtime contract.
    Контрольная точка: документировано, сколько backend worker processes
    допустимо запускать на CPU-only сервере с учетом памяти, физических cores,
    p95 latency и допустимого времени ожидания в локальной очереди.

12. Зафиксировать исследование альтернатив.
    Контрольная точка: отдельный request-embedding worker/service остается
    следующей задачей только если стенд показывает, что он не хуже backend
    workers по latency, памяти и backpressure. Без такого доказательства
    основной путь - bounded local queue внутри backend process.

## Verification

- `rg -n "asyncio.to_thread\\(.*embed_query|to_thread\\(\\s*embed_query" vchat`
  не показывает прямых request-path вызовов, кроме допустимых тестов или
  низкоуровневого helper-а.
- Unit-тест bounded helper-а доказывает лимит конкурентного encode.
- Unit-тест проверяет именно `max_active_encode == 1`, даже если одновременно
  запущено 10 coroutine-вызовов helper-а.
- Unit-тест или integration-test доказывает queue timeout/backpressure: лишние
  запросы не создают новый encode и получают предсказуемый результат.
- Benchmark/test harness фиксирует p50/p95 latency, RSS и max active encode для
  single process и multi-process режимов.
- Стенд проверяет, что dedicated thread для `model.encode()` не приводит к
  segfault, зависанию event loop или неконтролируемому росту памяти.
- Multi-process проверка показывает, как распределяются запросы между backend
  workers и где появляются очереди.
- Unit-тест `top_k`/RAG контекста продолжает проходить после замены вызова в
  `get_context()`.
- Guardrail/websocket тесты проходят и подтверждают сохранение `user_embedding`.
- Ошибка внутри `embed_query()` в тесте не проглатывается.
- Новая настройка имеет один канонический ключ в `vchat/config.yaml`; нет
  псевдонимов и environment-specific ветвлений в коде.
- Проверка launcher-тестов подтверждает, что новая web-логика не расходится с
  существующей серверной моделью embedder capacity:
  `tests/test_embedder_parallelism.py`.
- В ручной локальной проверке 10 конкурентных вызовов не увеличивают
  одновременный `model.encode()` внутри одного backend process выше `1`.
- Проверки запускаются через project venv:

```bash
venv/bin/pytest tests/chat/test_ctx_module.py tests/chat/test_guardrails_websocket.py tests/test_embedder_parallelism.py -q --no-cov
venv/bin/ruff check vchat/views/chat/ctx.py vchat/views/chat/views.py tests/chat/test_ctx_module.py tests/chat/test_guardrails_websocket.py
```

## Open Questions

- Подтверждаем ли базовое решение: `max_workers=1` и semaphore `1` внутри
  каждого backend process?
- Какой точный queue policy нужен при ожидании дольше `20-30` секунд: 503,
  websocket error event или иной существующий chat error path?
- Хотим ли сразу добавить метрику ожидания limiter-а, или для первой итерации
  достаточно structured log и benchmark output?
- Какой допустимый runtime contract для CPU-only сервера: сколько backend
  worker processes на host и сколько памяти нужно резервировать под каждый
  загруженный embedding model?
- Какие параметры тестового стенда считаются server-like: число cores, RAM,
  число backend workers, размер запроса, 10/50 одновременных websocket или HTTP
  запросов?
- Нужно ли для этой задачи писать отдельный benchmark script в `tests/` или
  `bin/`, чтобы будущие изменения embedding model проверялись тем же способом?
