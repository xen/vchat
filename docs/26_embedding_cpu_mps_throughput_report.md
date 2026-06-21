# Отчет по пропускной способности embedding модели на CPU/MPS

Дата замера: 2026-06-16

## Техническое резюме

Текущие замеры помечены в данных как `local baseline` для локальной машины и
`server run` для серверов. Локальный baseline выполнен на MacBook Pro с Apple
M2 Max, 12 CPU cores, 32 GB RAM и PyTorch MPS. Серверные замеры выполнены на
`S01`, `S02` и `S03` в
одноразовых окружениях под `/root/vchat_embedding_bench_20260616`; после
копирования результатов эти окружения и скачанные модели были удалены.

По single-message encode (`batch_size=1`) MPS на M2 Max остается самым быстрым
режимом: `0.75s` на 12k символов. Локальный CPU M2 Max занимает `2.96s` на том
же входе. Среди серверов лучший single-message результат у Threadripper 2950X:
`8.72s` на 12k. EPYC 7401P дает `10.53s`, а i7-3770 уходит в `38.36s`.

По параллельному crunching до `4000` символов лучший проверенный режим:

- Apple M2 Max MPS: raw-пик на `2` процессах, `13.41 embeddings/s`, около
  `2.5 GB` RSS; практическая зона `1-2` процесса.
- Apple M2 Max CPU: raw-пик на `10` процессах, `5.38 embeddings/s`, около
  `15.9 GB` RSS; практическая зона `8-10` процессов.
- Intel i7-3770: пик на `4` процессах, `0.40 embeddings/s`, около `7.0 GB` RSS.
- Threadripper 2950X: пик на `16` процессах, `2.39 embeddings/s`, около
  `27.1 GB` RSS.
- EPYC 7401P: raw-пик на `48` процессах, `1.51 embeddings/s`, около `79.4 GB`
  RSS, но с высокой задержкой; практическая зона для продукта скорее `16-24`
  процесса, `1.17-1.29 embeddings/s`, `26.9-40.4 GB` RSS.

Главный практический вывод: для интерактивного embedding одного сообщения MPS
сильно снижает задержку, но сама зависимость от размера входа остается заметной.
Для серверов без GPU/MPS CPU latency на длинных входах может стать основным
ограничением. Поэтому лимиты на размер сообщения и char/token-capped batching
остаются важными даже при ускорителе и критичны для CPU-only серверов.

Отчет состоит из двух частей: latency одного сообщения на CPU/MPS и
process-parallel capacity на CPU/MPS. Вторая часть намеренно измеряет не
мультитрединг модели, а независимые OS-процессы: каждый процесс загружает свою
модель, а PyTorch threads per process зафиксированы в `1`.

## Задержка растет вместе с размером входа на всех машинах

Сводный график строился по размеру одного синтетического русскоязычного
сообщения в символах и медианной wall-clock задержке
`model.encode([text], batch_size=1)` после warmup. Чем ниже линия, тем лучше.
Серверные линии CPU-only; локальная машина показана отдельно для CPU и MPS.
Сгенерированные PNG/SVG/CSV/JSON артефакты не хранятся в репозитории и
выводятся скриптами в `tmp/embedding_throughput_benchmark/results/`.

Ключевая форма зависимости одинаковая: длинные входы резко дороже. Но масштаб
разный. На 12k символов `S01` медленнее локального M2 Max CPU примерно
в `13.0x`, а медленнее локального M2 Max MPS примерно в `51.1x`.
`S02` и `S03` ближе друг к другу, но
Threadripper быстрее EPYC в этом single-message сценарии, несмотря на меньшее
число физических ядер. Это ожидаемо для `batch_size=1`: замер сильнее отражает
single-request CPU path, память и per-core поведение, чем суммарное число cores.

Локальный baseline отдельно показывает тот же рост latency вместе с размером
входа.

MPS дает устойчивый выигрыш уже на коротких сообщениях. При этом кривая не
становится плоской: длинные сообщения все равно дороже, а после `8192` символов
MPS throughput также заметно снижается. Это подтверждает, что узкое место
зависит не только от устройства, но и от длины входа, стоимости tokenization /
model sequence и фактической формы входного текста.

## Таблица результатов: локальный baseline

| Message chars | Tokens | CPU median, s | MPS median, s | MPS speedup | CPU chars/s | MPS chars/s |
| ------------: | -----: | ------------: | ------------: | ----------: | ----------: | ----------: |
|           256 |     65 |         0.107 |         0.018 |        6.0x |       2,389 |      14,225 |
|           512 |    128 |         0.134 |         0.025 |        5.3x |       3,822 |      20,094 |
|         1,024 |    262 |         0.177 |         0.039 |        4.5x |       5,770 |      25,998 |
|         2,048 |    522 |         0.325 |         0.077 |        4.2x |       6,300 |      26,756 |
|         4,096 |  1,051 |         0.849 |         0.141 |        6.0x |       4,826 |      29,014 |
|         8,192 |  2,101 |         1.794 |         0.425 |        4.2x |       4,567 |      19,267 |
|        12,000 |  3,073 |         2.959 |         0.751 |        3.9x |       4,056 |      15,969 |

Самый высокий MPS throughput по символам наблюдается в середине проверенного
диапазона, а не на самом большом входе. Для production batching это важно:
batch, который проходит по количеству chunks, все еще может быть медленным,
если суммарных символов много.

## Таблица результатов: серверы CPU-only

| Host  | CPU                                                 | Message chars | Median, s | Chars/s | Tokens/s |
| ----- | --------------------------------------------------- | ------------: | --------: | ------: | -------: |
| `S01` | Intel Core i7-3770, 4 cores / 8 threads             |           256 |     0.513 |     499 |      127 |
| `S01` | Intel Core i7-3770, 4 cores / 8 threads             |         4,096 |     8.770 |     467 |      120 |
| `S01` | Intel Core i7-3770, 4 cores / 8 threads             |        12,000 |    38.364 |     313 |       80 |
| `S02` | AMD Ryzen Threadripper 2950X, 16 cores / 32 threads |           256 |     0.437 |     586 |      149 |
| `S02` | AMD Ryzen Threadripper 2950X, 16 cores / 32 threads |         4,096 |     3.267 |   1,254 |      322 |
| `S02` | AMD Ryzen Threadripper 2950X, 16 cores / 32 threads |        12,000 |     8.724 |   1,375 |      352 |
| `S03` | AMD EPYC 7401P, 24 cores / 48 threads               |           256 |     1.774 |     144 |       37 |
| `S03` | AMD EPYC 7401P, 24 cores / 48 threads               |         4,096 |     6.141 |     667 |      171 |
| `S03` | AMD EPYC 7401P, 24 cores / 48 threads               |        12,000 |    10.529 |   1,140 |      292 |

## Максимальная параллельная пропускная способность CPU

Вторая серия замеров отвечает на вопрос, сколько независимых worker-процессов
имеет смысл запускать для embedding входов до `4000` символов. Это ближе к
продуктовой конфигурации Celery/worker pool, чем single-message latency:
несколько процессов параллельно crunch-ят разные сообщения, каждый процесс
держит свою копию модели в памяти.

Ключевой результат: оптимум не равен максимальному числу логических threads.
Apple M2 Max MPS достигает raw-пика уже на `2` процессах; дальше throughput не
растет, потому что ускоритель насыщается, а процессы начинают конкурировать.
Apple M2 Max CPU достигает raw-пика на `10` процессах, но `8` процессов дают
практически тот же throughput при меньшей памяти и latency. Threadripper 2950X
достигает максимума на `16` процессах, после чего throughput падает. EPYC 7401P
формально показывает raw-пик на `48` процессах, но это дорогая конфигурация по
памяти и задержке. Для product serving ее стоит рассматривать только если нужна
именно максимальная суммарная переработка и допустимы `~79 GB` RSS и
`p50 ~29s`.

Память растет почти линейно от числа процессов, потому что каждый процесс
загружает модель отдельно. Практическая оценка на этих wheel/runtime:
`~1.7 GB` RSS на один embedding-процесс плюс небольшой шум allocator/runtime.
На малых машинах это быстро становится вторым ограничителем после CPU.

| Runtime label | Tested workers | Best / practical workers | Throughput, embeddings/s | Peak RSS | Mean CPU | p50 latency | p95 latency |
|---|---:|---:|---:|---:|---:|---:|---:|
| Apple M2 Max MPS | 1, 2, 4, 6, 8 | 1-2 practical; 2 raw peak | 12.20-13.41 | 1.3-2.5 GB | 36-50% | 0.07-0.12s | 0.16-0.32s |
| Apple M2 Max CPU | 1, 2, 4, 6, 8, 10, 12 | 8-10 practical; 10 raw peak | 5.36-5.38 | 12.8-15.9 GB | 999-1005% | 0.91-1.30s | 3.36-4.16s |
| Intel i7-3770 3.4GHz CPU | 1, 2, 3, 4 | 4 | 0.40 | 7.0 GB | 400% | 6.22s | 28.39s |
| Threadripper 2950X 3.5GHz CPU | 1, 2, 4, 8, 12, 16, 24, 32 | 16 | 2.39 | 27.1 GB | 1604% | 3.87s | 15.79s |
| EPYC 7401P 2.0GHz CPU | 1, 2, 4, 8, 16, 24, 32, 48 | 16-24 practical; 48 raw peak | 1.17-1.29 practical; 1.51 raw peak | 26.9-40.4 GB practical; 79.4 GB raw peak | 1593-2416% practical; 4448% raw peak | 6.61-14.83s practical; 29.34s raw peak | 33.69-36.81s practical; 50.95s raw peak |

Локальный Apple M2 Max MPS является лучшим measured reference в этой серии:
`1-2` процесса дают больше throughput, чем все CPU-конфигурации, при меньшей
памяти и latency. Среди CPU-конфигураций лучший reference — Apple M2 Max CPU на
`8-10` процессах. Среди проверенных серверов самый чистый результат у
Threadripper: `16` процессов загружают примерно `16` физических cores, дают
максимальный measured throughput и не раздувают latency так резко, как
конфигурации выше. i7-3770 можно использовать только как малую/тестовую
CPU-only машину. EPYC 7401P имеет много RAM и cores, но по этой модели слабее
по полезной throughput-per-core; его преимущество проявляется только при очень
широкой параллели, где latency становится плохой.

## Эмуляция роста памяти

Отдельный sampling собирал aggregate RSS всех worker-процессов на протяжении
90-секундного run в лучшей throughput-конфигурации каждой CPU-группы.
Синтетический набор входов был разнообразным: тексты циклически меняли длину
`256`, `512`, `1024`, `2048`, `3072`, `4000` символов и содержимое, чтобы не
гонять один и тот же вектор.

За короткое окно `90s` явного линейного leak-per-embedding не видно: основной
скачок RSS происходит при загрузке модели и прогреве процессов, дальше память
обычно выходит на плато или растет слабо. Это не доказывает отсутствия утечки:
для подтверждения известного подтекания нужен long-run режим на десятки тысяч
embedding операций и несколько часов. Скрипт уже пишет memory samples, поэтому
его можно запускать тем же способом с большим `--duration-seconds` и более
частым/долгим sampling.

Машиночитаемые результаты второй части генерируются локально в
`tmp/embedding_throughput_benchmark/results/`.

## Область замера и определения

- Модель: `deepvk/USER-bge-m3`
- Путь модели: `data/models/user-bge-m3`
- Локальный runtime: macOS arm64, Python `3.11.11`, PyTorch `2.6.0`
- Серверный runtime: Linux x86_64, PyTorch `2.6.0+cpu`
- Измеренные устройства: локально `cpu` и `mps`, на серверах `cpu`
- CUDA: недоступна на всех измеренных серверах
- Вход: одно синтетическое русскоязычное сообщение на каждый целевой размер
- Операция: `SentenceTransformer.encode([text], normalize_embeddings=True, batch_size=1, show_progress_bar=False)`
- Warmup: `1` encode на каждую пару device/size перед измерением
- Повторы: `3` измеренных encode на каждую пару device/size
- Отчетная задержка: median wall-clock seconds после warmup

## Характеристики оборудования

| Host label            | Host           | CPU / accelerator                              | Cores / threads |      RAM | OS / runtime                                       |
| --------------------- | -------------- | ---------------------------------------------- | --------------: | -------: | -------------------------------------------------- |
| `local-m2-max`        | `Future.local` | Apple M2 Max CPU + PyTorch MPS                 |         12 / 12 |    32 GB | macOS 26.5, Python 3.11.11, torch 2.6.0            |
| `cdn-okumy`           | `S01`          | Intel Core i7-3770 CPU @ 3.40GHz               |           4 / 8 |  15.5 GB | Ubuntu Linux 5.15, Python 3.11.15, torch 2.6.0+cpu |
| `bear-infraforecast`  | `S02`          | AMD Ryzen Threadripper 2950X 16-Core Processor |         16 / 32 | 125.7 GB | Ubuntu Linux 6.8, Python 3.12.3, torch 2.6.0+cpu   |
| `trade-infraforecast` | `S03`          | AMD EPYC 7401P 24-Core Processor               |         24 / 48 | 503.8 GB | Ubuntu Linux 6.8, Python 3.12.3, torch 2.6.0+cpu   |

Token count здесь означает вывод tokenizer модели с `add_special_tokens=False`
и без truncation. На момент замера в проектном config были значения:

- `embedding_max_seq_length: 8192`
- `embedding_chunk_max_chars: 12000`
- `embedding_encode_batch_max_chars: 12000`

## Методология

Скрипт бенчмарка находится в
[embedding_throughput_benchmark.py](embedding_throughput_benchmark.py). Он
загружает тот же локальный путь модели, который использует приложение, через
`jobs.embedder.model.load_embedding_model()`, последовательно измеряет каждое
устройство, синхронизирует MPS вокруг timed sections и пишет машиночитаемые
артефакты в `tmp/embedding_throughput_benchmark/results/`.

Команда воспроизведения:

```bash
venv/bin/python docs/embedding_throughput_benchmark.py
```

Для серверов использовался автономный скрипт
[embedding_throughput_standalone_benchmark.py](embedding_throughput_standalone_benchmark.py),
который не импортирует кодовую базу `vchat`. На каждом сервере была создана
временная директория `/root/vchat_embedding_bench_20260616`, внутри нее:

- создано изолированное Python окружение;
- установлены `torch==2.6.0+cpu`, `sentence-transformers==5.2.0`,
  `transformers==4.57.6`, `psutil==7.1.3`;
- модель скачана через `SentenceTransformer("deepvk/USER-bge-m3")` в локальный
  cache внутри этой временной директории;
- после замеров результаты скопированы в
  `tmp/embedding_throughput_benchmark/results/server_runs/`;
- временная директория удалена на всех трех серверах.

Генератор объединенных артефактов:
[embedding_throughput_build_assets.py](embedding_throughput_build_assets.py).
Он создает объединенные CSV/JSON и PNG/SVG-графики в
`tmp/embedding_throughput_benchmark/results/`.

Параллельный benchmark находится в
[embedding_parallel_capacity_benchmark.py](embedding_parallel_capacity_benchmark.py).
Методика:

- запускаются независимые OS-процессы, а не threads;
- каждый процесс отдельно загружает `SentenceTransformer("deepvk/USER-bge-m3")`;
- внутри каждого процесса вызывается `torch.set_num_threads(1)` и
  `torch.set_num_interop_threads(1)`;
- устройство задается через `--device cpu` или `--device mps`; для MPS после
  каждого encode вызывается `torch.mps.synchronize()`;
- parent-процесс ждет, пока все workers загрузят модель и сообщат `ready`, и
  только после этого запускает timed measurement;
- входы разнообразятся по длине и тексту, верхняя граница `4000` символов;
- parent sampling пишет aggregate RSS и aggregate CPU percent дочерних
  процессов.

Типовая команда для конкретного сервера:

```bash
HF_HOME=/root/vchat_embedding_bench_20260616/hf_cache \
venv/bin/python embedding_parallel_capacity_benchmark.py \
  --host-label server-label \
  --device cpu \
  --output-dir results \
  --worker-counts 1 2 4 8 16 24 32 \
  --duration-seconds 90 \
  --max-chars 4000 \
  --sample-interval 5
```

Для поиска параметров продукта на новой машине нужно прогнать worker-counts
вокруг физических cores, затем выбрать не только raw throughput peak, но и
конфигурацию, где одновременно приемлемы:

- `embeddings_per_second`;
- `peak_total_rss_mb`;
- `mean_total_cpu_percent`;
- `p50_latency_seconds` и `p95_latency_seconds`;
- наличие запаса RAM под приложение, Celery, Redis/Postgres clients и OS cache.

## Интерпретация для vchat

Для локальной интерактивной embedding-нагрузки MPS стоит предпочитать, когда он
доступен. Он держит задержку одного сообщения ниже секунды до проверенного
лимита `12k` символов, тогда как CPU пересекает одну секунду между `4096` и
`8192` символами.

CPU остается пригодным для коротких сообщений, но становится дорогим для
длинного входа. На `12k` символов CPU single-message encode занимает около
`2.96s` даже на M2 Max CPU; для CPU-only серверов длинные payloads нужно
ограничивать еще жестче.

Старые reindex monitoring reports показывали тот же операционный вывод с другой
стороны: длинные batches по суммарным символам доминировали throughput, а
увеличение chunk count per batch не давало надежного улучшения completion time.
Этот локальный single-message benchmark поддерживает тот же следующий шаг:
выбор embedding batch должен ограничивать суммарные chars/tokens, а не только
количество chunks.

Для серверной CPU-only обработки Threadripper 2950X выглядит лучшим из трех
проверенных CPU под эту модель: он выигрывает и single-message latency, и
параллельный product-like throughput. EPYC 7401P имеет больше cores и RAM, но
полезный throughput растет хуже, а raw-пик требует слишком много процессов и
памяти. i7-3770 непригоден для длинных CPU-only embedding payloads без жесткого
ограничения размера входа или выноса embedding на более быструю машину.

## Ограничения и проверки устойчивости

- Синтетический текст контролирует размер входа, но не покрывает все реальные
  формы документов. HTML tables, повторенная навигация, code-like text и PDFs
  могут токенизироваться иначе.
- Single-message benchmark измеряет warm model encode latency, а не cold model load.
- Parallel benchmark исключает model load из timed window, но RSS учитывает уже
  загруженные процессы.
- Parallel benchmark намеренно фиксирует PyTorch threads per process в `1`; это
  не тест оптимального внутрипроцессного multithreading.
- RSS measurements показывают только process RSS. Поведение MPS allocator и
  GPU-side memory не полностью представлены process RSS.
- Три повтора достаточны, чтобы показать форму зависимости, но недостаточны для
  узких confidence intervals.
- Memory-growth run длиной `90s` не является доказательством отсутствия утечек;
  для leak hunting нужен long-run на существенно большем числе embeddings.
- Серверные окружения были временными и после замера удалены; повторный прогон
  заново скачает модель и может немного отличаться из-за системной нагрузки,
  throttling и версии wheel resolver.

## Рекомендуемые следующие шаги

1. Для CPU-only серверов начинать product-конфигурацию с process count около
   числа физических cores, а не logical threads.
2. Для Apple M2 Max MPS использовать `1-2` embedding worker-процесса: `2`
   дает raw-пик, но `1` уже держит большую часть throughput при минимальной
   памяти и latency.
3. Для Apple M2 Max CPU использовать `8-10` embedding worker-процессов как
   практическую зону; `10` дает raw-пик, но `8` почти не уступает и дешевле по
   RSS/latency.
4. Для Threadripper 2950X использовать `16` embedding worker-процессов как
   стартовую peak-конфигурацию; для EPYC 7401P отдельно решить, важнее raw
   throughput (`48`) или latency/RSS (`16-24`).
5. Добавить long-run memory leak benchmark на несколько часов с тем же
   diversified input generator.
6. Добавить batch-style benchmark для production batches: варьировать и
   `batch_size`, и total batch characters.
7. Держать `embedding_encode_batch_max_chars` и выбор pending chunks
   согласованными вокруг total-character или total-token cap.
8. Относиться к `12000` символов как к дорогой верхней границе, а не как к
   нормальной целевой форме входа.
9. Перезапускать этот benchmark после смены модели, PyTorch, macOS, hardware
   или chunking policy.

## Открытые вопросы

- Какой MPS batch size дает лучший throughput при фиксированном total batch
  characters?
- При каком total-token cap CPU становится операционно неприемлемым для
  server worker profile?
- Имеет ли реальный corpus text такой же chars-to-tokens slope, как этот
  синтетический русский вход?

## Планирование количества конфигураций под целевой throughput

Таблица ниже показывает, сколько одинаковых машин или запусков выбранной
конфигурации нужно, чтобы достичь целевой пропускной способности. Расчет:
`ceil(target_messages_per_second / measured_embeddings_per_second)`.

Это грубая capacity-planning оценка по чистому embedding crunching для входов до
`4000` символов. В ней нет запаса на очередь, базу данных, сериализацию,
сетевой overhead, рестарты worker-процессов, неравномерность входов и
production headroom. Для реального sizing стоит добавлять минимум `20-30%`
запаса сверх этой таблицы.

| Конфигурация | Запуск на 1 машине | Измеренный throughput | Peak RSS на 1 машину | 10 msg/s | 100 msg/s | 200 msg/s | 500 msg/s | 1000 msg/s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Apple M2 Max MPS | 2 процесса | 13.41 msg/s | 2.5 GB | 1 | 8 | 15 | 38 | 75 |
| Apple M2 Max CPU, raw peak | 10 процессов | 5.38 msg/s | 15.9 GB | 2 | 19 | 38 | 93 | 186 |
| Apple M2 Max CPU, practical | 8 процессов | 5.36 msg/s | 12.8 GB | 2 | 19 | 38 | 94 | 187 |
| Threadripper 2950X 3.5GHz CPU | 16 процессов | 2.39 msg/s | 27.1 GB | 5 | 42 | 84 | 209 | 418 |
| EPYC 7401P 2.0GHz CPU, raw peak | 48 процессов | 1.51 msg/s | 79.4 GB | 7 | 67 | 133 | 332 | 664 |
| EPYC 7401P 2.0GHz CPU, practical | 24 процесса | 1.29 msg/s | 40.4 GB | 8 | 78 | 156 | 389 | 778 |
| Intel i7-3770 3.4GHz CPU | 4 процесса | 0.40 msg/s | 7.0 GB | 25 | 250 | 499 | 1247 | 2494 |
