# Task: Безопасный локальный запуск Celery и индексации

## Goal

Локальные `make celery` и `make embedder` не создают неконтролируемую нагрузку
на память и PostgreSQL, а задачи повторной индексации используют план запроса,
масштабирующийся через индекс `chunk.page_id`.

## Context

- `jobs/crawler/tasks.py`: `refresh_project_index` ищет страницы без чанков и
  ставит их на повторную индексацию.
- `jobs/embedder/launcher.py`: launcher создаёт отдельные embedder worker-процессы.
- `vchat/config.yaml`, `vchat/settings.py`, `local.yaml`: настройки
  параллелизма Celery и embedder.
- Локальная база `vchat` содержит большие `page` и `chunk`; прежний запрос
  `refresh_project_index` выполнял глобальную агрегацию в ParadeDB/DataFusion.

## Current Behavior

- `embedding_worker_instances: auto` мог создавать до 11 процессов на CPU.
- Celery с concurrency 4 одновременно взял несколько `refresh_project_index`.
- Каждый такой запрос делал `JOIN` и `COUNT(chunk.id)` по всей выборке, а
  DataFusion строил крупный Hash Join в памяти PostgreSQL backend-а.

## Target Shape

- Безопасные локальные значения: один Celery task и один embedder worker.
- Дефолтный embedder не масштабируется по числу CPU без явной настройки
  конкретного окружения.
- Поиск страниц без чанков выражен через `NOT EXISTS` и использует
  `ix_chunk_document_id`.
- Контролируемый runtime-прогон имеет watchdog, снимает RSS и активные запросы,
  а после завершения не оставляет worker-ов или тяжёлых backend-ов.

## Guard Rails

- Не очищать Redis-очереди, не удалять данные и не перезапускать PostgreSQL
  ради проверки.
- Не менять production-параллелизм без отдельного замера и решения.
- Не выполнять повторный runtime-прогон без watchdog и заранее определённого
  условия остановки.
- Не смешивать с чужими изменениями в рабочем дереве.

## Iterations

1. Убрать глобальную hash-агрегацию из `refresh_project_index` и подтвердить
   планом `EXPLAIN` индексный Anti Join.
2. Установить безопасные дефолтные и локальные лимиты worker-ов; сохранить
   возможность явного override для измеренных окружений.
3. Добавить/обновить целевой тест контракта `refresh_project_index`, чтобы
   регрессия к глобальному агрегату была заметна при ревью.
4. Запустить локально Celery и embedder в отдельной process group с watchdog;
   подтвердить отсутствие worker-ов и активных тяжёлых backend-ов после stop.
5. Проверить все изменения изолированно, закоммитить только файлы задачи и
   приложить к issue результаты проверки.

## Verification

- `EXPLAIN` для поиска страниц без чанков содержит `Index Only Scan` по
  `ix_chunk_document_id` и не содержит ParadeDB/DataFusion Hash Join.
- Целевые pytest покрывают семантику повторной индексации; `py_compile` и
  `git diff --check` проходят.
- До и после runtime-прогона фиксируются количество worker-процессов, активные
  backend-ы `vchat`, RSS PostgreSQL и VM pressure.
- Успех: worker-процессы и тестовые backend-ы останавливаются в рамках watchdog,
  PostgreSQL не накапливает RSS после остановки, swap не растёт в устойчивом
  режиме.
- Неуспех: Hash Join/глобальная агрегация снова появляется в плане, остаются
  backend-ы после watchdog, либо наблюдается устойчивый рост RSS/swap.

## Open Questions

- Нужен ли отдельный production-лимит для одновременных
  `refresh_project_index`, или достаточно локального ограничения и текущей
  Redis-дедупликации?
