# Промпт для automaton: переход на Giga-Embeddings

Notion link: n/a

## Назначение

Этот документ подготовлен для переноса в репозиторий `automaton` и отдельного выполнения там.
Он не про `vchat`.
Его задача: перевести `automaton` с текущих embeddings на `Giga-Embeddings`, сохранив рабочий retrieval/runtime.

## Готовый промпт

Скопируй текст ниже и выполни его в репозитории `automaton`.

---

Нужно перевести `automaton` на `Giga-Embeddings` по образцу `vchat`, без изменения общей retrieval-архитектуры.

Контекст:

- Сейчас в `automaton` embeddings и warming завязаны на текущую модель.
- В `vchat` уже используется:
  - `embedding_model_id = ai-sage/Giga-Embeddings-instruct`
  - `vec_dim = 2048`
  - `embedding_max_seq_length = 4096`
  - `embedding_chunk_max_tokens = 3500`
  - `embedding_chunk_overlap_tokens = 400`
  - `embedding_chunk_max_chars = 12000`
- Нужно привести `automaton` к такому же embedding stack.

Что нужно сделать:

1. Найти все места, где в `automaton` зашита текущая embedding model и текущая размерность вектора.

2. Перевести их на:
   - `ai-sage/Giga-Embeddings-instruct`
   - `vec_dim = 2048`

3. Проверить и обновить:
   - конфиг проекта;
   - загрузку embedding model;
   - warming моделей;
   - миграции / модель `Chunk.embedding`, если размерность где-то зафиксирована явно;
   - все места, где есть предположение о старой размерности.

4. Не менять:
   - retrieval contract;
   - rerank model;
   - chunk types;
   - citations;
   - provider/runtime для generation.

5. Обновить `make setup`, чтобы он гарантированно выкачивал локально:
   - новый embedding model;
   - текущий rerank model.

6. Warming должен прогревать уже новый embedding model.

7. Проверить, что первый пользовательский запрос не пытается скачать embedding model лениво.

8. Добавить/обновить smoke checks:
   - модель грузится локально;
   - embedding размерности 2048;
   - индексатор не падает на новом embedder;
   - retrieval работает на новых embeddings.

9. Если старые embeddings несовместимы с новыми, не пытаться делать сложную совместимость.
   Нужен понятный путь полного re-embed/reindex.

Ожидаемый результат:

- `automaton` использует `Giga-Embeddings`;
- размерность вектора 2048;
- `make setup` всегда выкачивает нужные модели;
- warming проходит без ручных действий;
- код не содержит скрытых предположений о старой embedding model.

Перед изменениями:

- прочитай минимум:
  - `Makefile`
  - `entry.py`
  - `automaton/views/chat/ctx.py`
  - `jobs/embedder/tasks.py`
  - конфиг с embedding settings
  - модель `Chunk`

После изменений:

- коротко перечисли, какие файлы изменены;
- отдельно укажи, как теперь работает `make setup`;
- отдельно укажи, нужен ли полный reindex.

---

## Короткая памятка

Ключевые ожидания для `automaton`:

- embeddings -> `ai-sage/Giga-Embeddings-instruct`
- vector dim -> `2048`
- warming -> через `make setup`
- reranker -> остается, тоже warming через `make setup`
- старые embeddings можно считать одноразовыми данными и переиндексировать заново
