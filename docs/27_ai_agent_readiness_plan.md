# План реализации readiness AI-агента (`P0304-P0305`)

## Контекст

В проекте уже есть `GET /health/live`, `GET /health/ready` и legacy/frontend
`GET /check`. Runtime checks для оркестрации должны оставаться на
`/health/ready`, потому что этот endpoint уже используется в Compose и
Kubernetes readiness/startup probes.

`/check` не расширяется: он остается frontend smoke-check/redirect и не должен
становиться операционным readiness endpoint.

## Реализация

1. Расширить существующий `GET /health/ready`, не вводя второй параллельный
   endpoint.
2. Вернуть структурированный JSON вида:

   ```json
   {
     "status": "ok",
     "checks": {
       "database": {"status": "ok"},
       "redis": {"status": "ok"},
       "celery_broker": {"status": "ok"},
       "celery_workers": {"status": "ok"},
       "embedder": {"status": "ok"},
       "embedding_model": {"status": "ok"},
       "llm": {"status": "ok"}
     }
   }
   ```

3. При любой критичной ошибке возвращать HTTP 503 и `status: "degraded"`.
4. Проверять PostgreSQL быстрым `select 1`.
5. Проверять app Redis через `request.app[REDIS_KEY].ping()`.
6. Проверять Celery broker отдельно от app Redis: подключаться к
   `celery_redis_uri + celery_broker_db`, выполнять `PING`, читать длины
   очередей `celery`, `crawler`, `embeddings`.
7. Проверять Celery workers через `jobs.celery.app.control.inspect()` с малым
   таймаутом. Минимальный критерий: есть worker для default queue.
8. Проверять embedder как отдельный компонент через наличие worker, слушающего
   очередь `embeddings`. Пустая очередь не считается ошибкой.
9. Проверять локальную модель без inference на каждый probe: каталог
   `embedding_model_dir` существует, device разрешается через
   `resolve_embedding_device()`, web startup уже прогрел embedding и reranker
   модели.
10. Проверять LLM как конфигурационную готовность: выбранные `chat_provider` и
    `chat_model` разрешаются существующими helpers, provider поддерживает chat,
    API key для выбранного provider задан.
11. Не выполнять внешний chat completion в readiness, чтобы probe не зависел от
    тарификации, rate limits и latency внешнего LLM API.
12. Покрыть unit-тестами успешный JSON, отсутствие worker на `embeddings` и
    невалидную LLM-конфигурацию. Все внешние зависимости мокируются.

## Текущее состояние

План реализован в `vchat/views/health.py`.

`GET /health/live` проверяет только живость web-процесса.

`GET /health/ready` проверяет готовность AI-агента и его зависимостей:

- PostgreSQL;
- app Redis;
- Celery broker Redis;
- Celery worker для default queue;
- embedder worker на очереди `embeddings`;
- локальные embedding/rerank модели;
- LLM provider/model/API key.

HTTP 503 означает, что процесс нельзя вводить в балансировку до устранения
проблемы с зависимостью.
