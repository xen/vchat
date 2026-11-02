# Изолированные нагрузочные испытания VChat

Это отдельные процессы тестов. Runtime VChat их не импортирует. Доступ разрешён
только к loopback; секреты GigaChat заменены явно тестовыми. Защиты включены.
Подробные результаты: `docs/reports/2026-11-02-vchat-loadtest.md`.

## Подготовка среды

Нужны проектный `venv`, PostgreSQL с vector/pg_search, Redis с DB 40–42,
собранные frontend/frontend_chat и локальные веса embedding/reranker. Версии,
хэши весов и схема измеренного стенда находятся в `environment.json` и
`schema.sql` внутри `docs/reports/artifacts/vchat-loadtest-20261102/`.
Штатные пути: `models/reranker`, а у отдельного dzen_embedder — собственный
`models/user-bge-m3`. В опубликованном прогоне использованы существующие локальные
веса из соседних репозиториев через символическую ссылку; копирование не нужно.

Ниже команды для **новой пустой тестовой БД**, не для рабочей базы VChat:

```sh
createdb vchat_loadtest_20261102
psql -v ON_ERROR_STOP=1 postgresql://xen@127.0.0.1:5432/vchat_loadtest_20261102 \
  -f docs/reports/artifacts/vchat-loadtest-20261102/schema.sql
source tests/load/environment.env
venv/bin/python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base")'
# До запрета внешних соединений: это словарь токенизации, не вызов LLM.
```

В отдельном терминале каталога dzen_embedder запустить его собственным venv:

```sh
PORT=18191 DEVICE=mps DASHBOARD_ENABLED=false HF_HUB_OFFLINE=1 \
  venv/bin/dzen-embedder-serve
```

MPS — измеренная конфигурация Apple Silicon. Для CPU/CUDA нужно явно изменить
`RERANKER_DEVICE` и `DEVICE` и опубликовать новый результат для этой среды.

В каталоге VChat:

```sh
source tests/load/environment.env
venv/bin/python -m tests.load.stand seed
venv/bin/pytest -q tests/load/test_contract.py
venv/bin/python -m tests.load.run connections --embedder-pid EMBEDDER_PID \
  --output /tmp/vchat-load-connections
venv/bin/python -m tests.load.run chat --embedder-pid EMBEDDER_PID \
  --output /tmp/vchat-load-chat
# Удаляются только сгенерированные сообщения этого изолированного стенда.
# Сначала сохранить предыдущие артефакты; рабочую БД здесь не указывать.
psql -v ON_ERROR_STOP=1 postgresql://xen@127.0.0.1:5432/vchat_loadtest_20261102 \
  -c 'TRUNCATE chat_msg RESTART IDENTITY;'
venv/bin/python -m tests.load.run combined --embedder-pid EMBEDDER_PID \
  --output /tmp/vchat-load-combined
```

`seed` отказывается работать с непустыми таблицами. Генератор отказывается
перезаписывать каталог измерений. Новый прогон должен начинаться без истории
сообщений; корпус и 1000 тестовых чатов сохраняются. DB 40 выделена стенду,
DB 41/42 зарезервированы, Celery в этих сценариях не запускается.

## Измерения и границы

Соединения открываются максимум по 50 одновременно; каждое проходит реальную
проверку подписанного payload/чата в БД и прикладной ping/pong. Чатовые сообщения
выпускаются общей заслонкой, а реальная конкурентность доказывается интервалами
`request_start`/`request_end` на сервере. Подготовка подписанных тестовых payload
заменяет только пользовательский вход через браузер; UI/login/TLS/proxy не измерены.
Удержание — 30 секунд после подключения последнего клиента. В combined измеряется
удержание до конца волны сообщений и повторного ping/pong, а не отдельные 30 секунд.

Первый токен означает получение первого непустого содержимого `partial=true`,
а не точную границу токенизатора. Полный ответ — финальный `partial=false` после
сохранения. Подсказки идут после финального ответа; ошибки подсказок видны в
сырых кадрах/логах, а их задержка не включена в полный ответ. Таймаут клиента
120 секунд; штатные таймауты приложения не изменяются. p50/p90/p95 — nearest rank,
только по успешным ответам; без успехов значения отсутствуют. Время ошибки
публикуется отдельно и не считается временем успешного ответа.

`resources.jsonl` снимается каждые 0,5 секунды независимым клиентским процессом:
CPU/RSS/FD/threads клиента, сервера, эмулятора, процесса embedding, доступная RAM
и load average всей машины. Первый cpu_percent у psutil не имеет базы и равен 0.
GPU-память, дочерние процессы PostgreSQL/Redis и внутренний scheduler embedding
отдельно не профилируются. `server.jsonl` содержит занятость пула БД, задержку
цикла событий, длительности этапов, embedding encode/queue и reranking. Синхронный
reranking оставлен в штатном потоке, поэтому лаг наблюдателя тоже значим.

Эмулятор: OAuth `/oauth` с Basic `loadtest-only`, bearer `loadtest-bearer`,
`/api/v1/chat/completions` с SSE; 0,5 с перед первым фрагментом, фрагменты по
64 символа с паузой 0,05 с, 1984 символа / 3712 байт UTF-8. Поле usage=512 —
синтетическая величина, а не результат токенизации. Классификаторы, moderation
и Responses API не имитируются успешными решениями: нестриминговые completions
и неизвестные маршруты возвращают 501. Это явная граница измерений при
несовместимом протоколе Guardrails, а не обход защиты.

Сетевой запрет охватывает клиент и сервер VChat. Сервис embedding из отдельного
репозитория запускается с локальными весами/offline. Никаких реальных ключей
или вызовов GigaChat не требуется. Тестовый сервер завершает только свои
процессы перед закрытием клиентских сокетов: штатный WebSocket VChat в измеренной
версии не выходит на CLOSED. Это ограничение очистки стенда, не исправление runtime.

## Полный набор перед публикацией

Для smoke-тестов HTML у тестового виджета обязательно непустое
`welcome_messages`; seed создаёт его явно. На версии после интеграции изменения
блокировки пользователей схема проверочной БД также должна быть актуальной.
Использованный отдельный контур проверок: `vchat_loadtest_checks_20261102`, Redis
43/44/45. Он отделён от снимка измеренной нагрузки.

```sh
DATABASE_URI=postgresql+asyncpg://xen@127.0.0.1:5432/vchat_loadtest_checks_20261102 \
REDIS_URI=redis://127.0.0.1:6379/43 CELERY_BROKER_DB=44 CELERY_BACKEND_DB=45 \
RUN_LIVE_LLM_TESTS=0 GIGACHAT_API_KEY=loadtest-only make test
```
