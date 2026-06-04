# Embedding Optimization Report

Дата: 2026-06-04

## Контекст

Изначальная проблема: при параллельном запуске нескольких `make embedder` процессов воркеры выбирали одни и те же документы/chunks и дублировали работу. После попыток локальной параллельной обработки на MPS система начала деградировать: музыка переставала работать, kernel/WindowServer и Python создавали сильную нагрузку, появлялся высокий memory pressure/swap.

Дополнительное ограничение от владельца: локально не запускать CPU embedder workers, только MPS.

На момент остановки обработки из-за блокеров:

- фоновых embedder-процессов нет;
- `pending_chunks = 15685`;
- `docs_with_pending = 1456`;
- duplicate `(page_id, chunk_ix)` keys = `0`.

Проверка процессов выполнялась через:

```bash
ps -axo pid,ppid,%cpu,%mem,etime,command | rg 'celery -A jobs\.celery worker.*-Q embeddings|make embedder|python.*jobs\.embedder|vchat_profile_embedder_batches'
```

Проверка базы выполнялась через:

```bash
psql postgresql://xen@localhost:5432/vchat -c "
select count(*) as pending_chunks, count(distinct page_id) as docs_with_pending
from chunk
where embedding is null;

select count(*) as duplicate_page_chunk_keys
from (
  select page_id, chunk_ix
  from chunk
  where page_id is not null
  group by page_id, chunk_ix
  having count(*) > 1
) d;"
```

## Реализованные изменения

### 1. Защита от дублирования materialization документов

В [jobs/crawler/tasks.py](/Users/xen/Dev/sber/vchat/jobs/crawler/tasks.py) добавлена защита уровня документа:

- document-level advisory transaction lock через `pg_try_advisory_xact_lock(namespace, page_id)`;
- namespace теперь вычисляется детерминированно по имени через `blake2s`, без магических чисел;
- параллельный `index_document(page_id)` при занятом lock пропускает документ, а не материализует chunks повторно;
- в `Page.meta` сохраняется `embedding_index_content_hash`;
- если chunks уже соответствуют текущему content hash, документ не материализуется повторно;
- stale chunks удаляются и создаются заново только при изменении content hash.

Это закрывает главный race, из-за которого несколько crawler/indexer инстансов могли одновременно пересоздавать chunks одного документа.

### 2. Защита от дублирования embedding chunks

В [jobs/embedder/tasks.py](/Users/xen/Dev/sber/vchat/jobs/embedder/tasks.py) batch processor выбирает pending chunks через:

- `FOR UPDATE SKIP LOCKED`;
- сортировку по `page_id`, `chunk_ix`;
- table-level updates без ORM hydration;
- обработку dangling chunks;
- проверку oversized token count до вызова модели.

Это позволяет нескольким embedder workers брать разные строки из `chunk`, не блокировать друг друга и не писать embedding для одной и той же строки одновременно.

### 3. Batch embedding вместо single-chunk encode

Добавлен `make_embed_vectors(texts)`:

- adaptive splitting по `embedding_encode_batch_max_chars`;
- `show_progress_bar=False`, чтобы убрать tqdm/log spam;
- vector conversion через `numpy.asarray`;
- NaN validation через `np.isnan(vectors).any()`.

`process_pending_chunk_batch()` теперь кодирует batch chunks за один проход и пишет embeddings bulk update-ом.

### 4. Убраны дорогие `count(*)` из hot path

`run_pending_chunk_batch()` больше не считает точный остаток после каждого batch. Вместо этого используется cheap existence check:

```python
select Chunk.id where Chunk.embedding is null limit 1
```

Точный `count_pending_chunks()` оставлен только для scheduler target calculation.

### 5. Chunking вынесен из ML worker path

Созданы новые модули:

- [jobs/embedder/chunking.py](/Users/xen/Dev/sber/vchat/jobs/embedder/chunking.py)
- [jobs/embedder/queue.py](/Users/xen/Dev/sber/vchat/jobs/embedder/queue.py)
- [vchat/embedding_tokenizer.py](/Users/xen/Dev/sber/vchat/vchat/embedding_tokenizer.py)

Результат:

- `jobs.crawler.tasks` больше не импортирует `jobs.embedder.tasks`;
- crawler/chunking path больше не грузит `torch`;
- crawler/chunking path больше не грузит `transformers`;
- crawler/chunking path больше не грузит `sentence_transformers`;
- для chunking используется лёгкий `tokenizers.Tokenizer.from_file`.

Проверка:

```bash
venv/bin/python -c "
import jobs.crawler.tasks, sys
print('sentence_transformers', 'sentence_transformers' in sys.modules)
print('torch', 'torch' in sys.modules)
print('transformers', 'transformers' in sys.modules)
print('tokenizers', 'tokenizers' in sys.modules)
"
```

Результат:

```text
sentence_transformers False
torch False
transformers False
tokenizers True
```

### 6. Fail-fast для явно запрошенного устройства

В [vchat/embeddings.py](/Users/xen/Dev/sber/vchat/vchat/embeddings.py) изменено поведение `resolve_embedding_device()`:

- `EMBEDDING_DEVICE=mps` больше не падает молча на CPU, если MPS недоступен;
- `EMBEDDING_DEVICE=cuda` больше не падает молча на CPU, если CUDA недоступна;
- явный unavailable device теперь вызывает `RuntimeError`.

Это важно из-за требования запускать локально только MPS и не получать CPU worker случайно.

### 7. Oversized documents

Для oversized documents добавлена fail-fast/skip логика:

- `embedding_document_max_chars: 1000000`;
- oversized document получает `status_error=too_big`;
- chunks oversized документа удаляются;
- chunking не вызывается для oversized content.

Отдельно был найден документ:

- `page_id=12121`;
- URL: `https://ai-academy.ru/upload/csv/dota2_skill_train.csv`;
- размер около 22 MB;
- раньше имел 7513 chunks и зависал в `parsing`;
- после cleanup переведён в ошибку размера, chunks удалены.

Админские ссылки:

- `https://local.vchat.com/page/12121`
- `https://local.vchat.com/page/12121/content`
- `https://local.vchat.com/source/5`

## Профилирование

Установлены инструменты в project venv:

- `pyinstrument`;
- `memray`;
- `memory-profiler`;
- `scalene`.

Установка выполнялась через активированный project venv и `uv pip install`.

### Chunking CPU profile

До разделения модулей:

- chunking импортировал `jobs.embedder.tasks`;
- это тянуло `vchat.embeddings`;
- далее грузились `torch`, `transformers`, `sentence_transformers`;
- `memray` показывал примерно `319 MB` allocations на chunking profile, основная часть была import overhead.

После разделения:

- chunking того же документа `page_id=12007`, `124654 chars`, `226 chunks`;
- wall time самого chunking около `0.35s`;
- `memray` allocations около `90 MB`;
- основные allocations остались в импортах ORM/model utilities и загрузке `tokenizer.json`, а не в ML stack.

### Embedder MPS profile

Профили `pyinstrument` показали:

- hot path почти полностью внутри `SentenceTransformer.encode`;
- Python/SQL overhead после batch processing небольшой;
- cold import/model load остаётся дорогим, но нужен в embedder process;
- reset модели дорогой и не всегда сразу освобождает RSS из-за поведения MPS/PyTorch allocator.

Пример bounded MPS прогонов:

- batch size 4: стабильно, но медленно;
- batch size 8: лучший throughput среди проверенных локальных bounded режимов;
- batch size 16: memory profile мягче в одном тесте, но throughput хуже на текущих данных;
- long single-process batch-8 с reset threshold обработал 408 chunks за 180 секунд, но reset по RSS освобождал память не мгновенно.

Вывод: для локального доведения очереди безопаснее использовать bounded single-MPS процессы и process recycling, а не несколько параллельных MPS процессов.

## Проверки

Полный тестовый прогон после изменений:

```bash
venv/bin/pytest -q
```

Результат:

```text
432 passed, 2 skipped, 2 warnings
```

Линтер/формат:

```bash
venv/bin/ruff check ...
venv/bin/ruff format --check ...
```

Результат:

```text
All checks passed
```

## Runtime progress по embeddings

Очередь продвигалась только bounded MPS-прогонами. CPU workers локально не запускались.

За время работ было обработано несколько сотен chunks, включая последние bounded runs:

- batch size 8, 30 итераций: `240` chunks;
- batch size 16, 10 итераций: `160` chunks;
- batch size 8, 30 итераций: `240` chunks;
- long batch size 8 до 180 секунд: `408` chunks.

Текущее состояние после остановки:

```text
pending_chunks = 15685
docs_with_pending = 1456
duplicate_page_chunk_keys = 0
```

## Блокеры и риски

### 1. Локальная параллельная MPS обработка небезопасна

Запуск 2-3 локальных MPS процессов уже приводил к деградации системы. Даже один bounded MPS process на batch size 8 может поднять RSS выше 1 GB на тяжелом участке данных.

Текущий вывод:

- локально не запускать 2-3 MPS embedder процесса;
- локально не запускать CPU embedder процессы;
- для локального добивания очереди использовать single-MPS bounded process;
- для production/server обработки можно использовать несколько workers только после проверки памяти на целевой машине.

### 2. `reset_embed_model()` не является мгновенным memory fix

Эксперимент с reset threshold показал:

- reset модели после превышения RSS может не сразу снижать RSS;
- MPS/PyTorch allocator освобождает память не всегда синхронно с Python `del/gc`;
- process recycling надёжнее, чем частые reset внутри одного процесса.

### 3. Очередь ещё не доведена до нуля

Цель полного создания embedding vectors не закрыта:

- `pending_chunks` всё ещё `15685`;
- требуется продолжить обработку после снятия блокеров;
- completion нельзя считать достигнутым до `pending_chunks = 0`.

### 4. В Redis остаются старые queued tasks

На момент проверок:

- Redis broker DB 31;
- `LLEN embeddings = 103`;
- `vchat:embed:pending_chunks:inflight` отсутствует (`TTL = -2`).

Эти queued tasks не являются дублями chunks сами по себе, но перед production запуском стоит решить, нужно ли их оставить или пересоздать scheduler state.

## Рекомендованный план продолжения

1. Не запускать локально 2-3 MPS процесса.
2. Если нужно продолжать локально, использовать bounded single-MPS process:

```bash
PYTHONPATH=/Users/xen/Dev/sber/vchat \
EMBEDDING_DEVICE=mps \
OMP_NUM_THREADS=1 \
MKL_NUM_THREADS=1 \
OPENBLAS_NUM_THREADS=1 \
NUMEXPR_NUM_THREADS=1 \
VECLIB_MAXIMUM_THREADS=1 \
TOKENIZERS_PARALLELISM=false \
PROFILE_BATCH_ITERS=30 \
PROFILE_BATCH_SIZE=8 \
venv/bin/python /tmp/vchat_profile_embedder_batches.py
```

3. После каждого bounded run проверять:

```bash
ps -axo pid,ppid,%cpu,%mem,etime,command | rg 'celery -A jobs\.celery worker.*-Q embeddings|make embedder|python.*jobs\.embedder|vchat_profile_embedder_batches'
```

4. После каждого run проверять БД:

```bash
psql postgresql://xen@localhost:5432/vchat -c "
select count(*) as pending_chunks, count(distinct page_id) as docs_with_pending
from chunk
where embedding is null;

select count(*) as duplicate_page_chunk_keys
from (
  select page_id, chunk_ix
  from chunk
  where page_id is not null
  group by page_id, chunk_ix
  having count(*) > 1
) d;"
```

5. На тестовом сервере запускать embedder service только после деплоя кода и проверки, что:

- `EMBEDDING_DEVICE` задан явно;
- service не падает на CPU fallback;
- memory pressure контролируется;
- `pending_chunks` монотонно уменьшается;
- duplicate `(page_id, chunk_ix)` остаётся `0`.

## Статус цели

Не завершена.

Доказано:

- дублирование chunks на уровне `(page_id, chunk_ix)` сейчас отсутствует;
- кодовая защита от параллельной materialization добавлена;
- batch embedding через `FOR UPDATE SKIP LOCKED` добавлен;
- crawler больше не грузит ML stack;
- CPU fallback для `EMBEDDING_DEVICE=mps` закрыт fail-fast поведением;
- тесты проходят.

Не доказано / не выполнено:

- `pending_chunks` не равен `0`;
- полный embedding corpus ещё не создан;
- локальный запуск 2-3 MPS процессов признан небезопасным текущими измерениями и остановлен.
