# Request-path embedding runtime benchmark

Дата: 2026-07-04

## Короткий вывод

Для `bear.infraforecast.com` с CPU `AMD Ryzen Threadripper 2950X`, 16 physical
cores / 32 logical threads, и выбранной embedding-моделью
`deepvk/USER-bge-m3` наиболее практичная конфигурация для request-path
embedding:

- `1` backend worker process, если важнее простота, память и низкая tail
  latency.
- `2` backend worker processes за nginx, если нужно немного больше concurrent
  capacity и допустимы `~3.6 GB` RSS только под две копии embedding model.
- Внутри каждого backend process: только `1` активный `encode()` через
  локальный bounded helper; очередь ожидания ограничивать `20s`.
- Для CPU inference на этом host: `torch.set_num_threads(16)` на process дал
  лучший single-request latency, но два process по `16` threads уже конкурируют
  за CPU. Это работает до умеренной нагрузки, но не является бесплатным
  масштабированием.

Рекомендуемый стартовый runtime contract:

```text
backend workers: 1-2 на host
active request embeddings per process: 1
torch intra-op threads per process: 16 на bear-class CPU
queue timeout: 20s
expected RSS per loaded embedding model: 1.8 GB
expected safe concurrency:
  1 worker: до ~12 concurrent requests без 503, но p95 уже ~17s
  2 workers через nginx: до ~12 concurrent requests с p95 ~16s
failure point:
  1 worker: concurrency 16 дает 503
  2 workers: concurrency 24 дает массовые 503
```

Если продуктово нужно держать p95 ниже `10-12s`, рабочая зона уже меньше:

- `1` worker: concurrency до `8`;
- `2` workers: concurrency до `8`.

## Почему так

Интернет-ресерч совпал с измерениями:

- PyTorch CPU inference имеет intra-op / inter-op thread pools; `torch.set_num_threads()` управляет intra-op parallelism и должен вызываться до workload. См. PyTorch docs: https://docs.pytorch.org/docs/stable/generated/torch.set_num_threads.html
- PyTorch docs по threading environment variables отдельно указывают `OMP_NUM_THREADS` / `MKL_NUM_THREADS`; oversubscription может ухудшить latency. См. https://docs.pytorch.org/docs/stable/threading_environment_variables.html
- Nginx `least_conn` отправляет запрос на upstream с меньшим числом активных connections. См. https://nginx.org/en/docs/http/load_balancing.html

Измерения показали главный tradeoff: один `encode()` становится быстрее при
увеличении PyTorch threads, но несколько backend processes начинают
конкурировать за те же CPU cores.

## Стенд

Код стенда:

- `data/embedder-test/embedder_request_bench.py`

Локальные результаты:

- `data/embedder-test/bear-vchat_embedder_request_bench_20260703_192102/results/`

На сервере использовался временный runtime:

- `/root/vchat_embedder_request_bench_20260703_192102`

После копирования результатов runtime был удален с сервера.

Стенд проверяет:

- direct encode;
- bounded thread executor внутри одного process;
- backend-like `aiohttp` worker с `Semaphore(1)` и `ThreadPoolExecutor(max_workers=1)`;
- несколько backend-like workers за временным nginx;
- `least_conn` и `round_robin`;
- latency, wait time, queue depth, status codes, worker distribution, RSS/CPU samples.

Все запросы использовали сообщения до `4000` символов и embedding-модель
`deepvk/USER-bge-m3`.

## Single encode: PyTorch threads

Steady-state после загрузки модели:

| torch threads | encode p50, s | Комментарий                       |
| ------------: | ------------: | --------------------------------- |
|             1 |         12.23 | слишком медленно для request path |
|             2 |          6.35 | лучше, но все еще дорого          |
|             4 |          4.71 | приемлемее                        |
|             8 |          1.83 | хороший latency                   |
|            16 |          1.40 | лучший single-process latency     |

Первый запрос process включал загрузку модели и был существенно дороже; для
production это означает, что backend worker должен прогревать embedding model
при старте, если request path зависит от local encode.

## Один backend worker

Конфигурация:

```text
workers: 1
torch threads: 16
active encode per process: 1
queue timeout: 20s
requests per step: 32
```

| Concurrency |   OK |  503 | latency p95, s | wait p95, s | encode p50, s |
| ----------: | ---: | ---: | -------------: | ----------: | ------------: |
|           1 |   32 |    0 |           1.72 |        0.00 |          1.39 |
|           2 |   32 |    0 |           3.03 |        1.58 |          1.39 |
|           4 |   32 |    0 |           6.14 |        4.74 |          1.40 |
|           8 |   32 |    0 |          11.50 |       10.10 |          1.39 |
|          12 |   32 |    0 |          16.90 |       15.50 |          1.38 |
|          16 |   30 |    2 |          21.17 |       19.89 |          1.38 |

Точка отказа: `concurrency=16` при `queue_timeout=20s`.

## Два workers за nginx

### `torch_threads=8`, nginx `least_conn`

| Concurrency |   OK |  503 | latency p95, s | wait p95, s | encode p50, s |
| ----------: | ---: | ---: | -------------: | ----------: | ------------: |
|           2 |   48 |    0 |           3.39 |        0.00 |          2.54 |
|           4 |   48 |    0 |           6.27 |        3.30 |          2.59 |
|           8 |   48 |    0 |          12.59 |        9.93 |          2.60 |
|          12 |   48 |    0 |          18.29 |       15.58 |          2.54 |
|          16 |   47 |    1 |          22.18 |       19.78 |          2.40 |
|          24 |   30 |   18 |          22.72 |       20.00 |          2.71 |

Точка отказа: первые 503 на `concurrency=16`, массовый отказ на `24`.

### `torch_threads=16`, nginx `least_conn`

| Concurrency |   OK |  503 | latency p95, s | wait p95, s | encode p50, s |
| ----------: | ---: | ---: | -------------: | ----------: | ------------: |
|           2 |   32 |    0 |           2.75 |        0.00 |          2.68 |
|           4 |   32 |    0 |           5.43 |        2.74 |          2.68 |
|           8 |   32 |    0 |          10.95 |        8.22 |          2.70 |
|          12 |   32 |    0 |          16.42 |       13.67 |          2.70 |
|          16 |   32 |    0 |          21.95 |       19.21 |          2.72 |
|          24 |   16 |   32 |          23.08 |       20.01 |          5.74 |

Точка отказа: `concurrency=24`. `concurrency=16` формально без 503, но p95
уже `~22s`, то есть это край, а не нормальная рабочая зона.

### `least_conn` vs `round_robin`

На двух одинаковых workers разницы почти нет:

| Method      | Concurrency |   OK |  503 | latency p95, s | Distribution |
| ----------- | ----------: | ---: | ---: | -------------: | ------------ |
| least_conn  |          12 |   32 |    0 |          16.42 | 16 / 16      |
| round_robin |          12 |   32 |    0 |          16.02 | 16 / 16      |
| least_conn  |          16 |   32 |    0 |          21.95 | 16 / 16      |
| round_robin |          16 |   32 |    0 |          22.01 | 16 / 16      |

Для одинаковых workers и равномерных запросов round-robin достаточно. `least_conn`
оставить разумно, потому что реальные запросы будут разной длины, а активные
соединения лучше отражают занятую очередь.

## Память и утечки

RSS на одну загруженную модель: примерно `1.8 GB`.

Stability-run:

```text
workers: 2
torch threads: 16
concurrency: 8
requests: 160
result: 160/160 OK
distribution: 80 / 80
latency p95: 11.10s
encode p95: 2.80s
max response RSS per worker: 1.803 GB
```

RSS samples:

```text
first total RSS: 3365.5 MB
middle total RSS: 3601.5 MB
last total RSS: 3606.5 MB
last 10 samples delta: 0.2 MB
after warmup min/max: 3597.2 / 3632.5 MB
```

Вывод: на серии `160` запросов явной утечки не видно. Есть рост после старта и
прогрева allocator/model runtime примерно на `230-270 MB` суммарно для двух
workers, затем память выходит на плато. Для окончательного доказательства
нужен long-run на тысячи запросов, но текущий тест не показывает runaway leak.

## Что это значит для кода

Нужен bounded helper в request path:

```text
embed_query_async(text)
  semaphore: 1
  executor: ThreadPoolExecutor(max_workers=1)
  queue timeout: 20s
  no silent fallback
  log wait_s, encode_s, queue_depth, timeout
```

Не надо запускать несколько `encode()` одновременно внутри одного backend
process. Это не дало бы контролируемого throughput и сделало бы память/CPU
хуже. Масштабирование должно идти process-level, но очень осторожно: каждая
копия модели стоит `~1.8 GB`, а два workers уже потребляют до `~26-29 CPU
threads` во время encode на `torch_threads=16`.

## Рекомендуемая первая конфигурация

Для `bear`-класса CPU:

```text
backend workers: 1
torch threads: 16
embedding active per process: 1
queue timeout: 20s
```

Если нужно больше capacity:

```text
backend workers: 2 behind nginx
nginx upstream: least_conn
torch threads: 16
embedding active per process: 1
queue timeout: 20s
```

Но считать safe рабочей зоной стоит не больше `concurrency=8` для p95 около
`11s`. `concurrency=12` уже крайняя зона (`~16s p95`), `16+` - degrade mode,
`24` - отказ.

## Следующие проверки

- Long-run на `1000-3000` запросов при `workers=2`, `concurrency=8`, чтобы
  окончательно проверить RSS plateau.
- Повторить тест после реализации `embed_query_async()` уже внутри реального
  backend request path.
- Добавить метрики `embedding_wait_seconds`, `embedding_encode_seconds`,
  `embedding_queue_depth`, `embedding_queue_timeout_total`.
- Проверить реальный websocket/chat path отдельно: LLM latency может маскировать
  embedding queue, но CPU contention останется.
