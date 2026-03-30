# OpenAPI спецификация API VChat

## 1. Назначение

Документ описывает публичные API-методы проекта, необходимые для:

- синхронизации/индексации документов по URL.

Все остальные API-методы из этой спецификации удалены как неактуальные.

## 2. OpenAPI 3.0 (YAML)

```yaml
openapi: 3.0.3
info:
  title: VChat Public API
  version: 1.0.0
  description: |
    Минимальный публичный API:
    1) /api/update — обновление индекса по URL документа.
servers:
  - url: https://chat.vbudushee.ru
    description: Production

tags:
  - name: Indexing
    description: Синхронизация документов в индексе

components:
  schemas:
    IndexUpdateResponse:
      type: object
      required:
        - status
        - url
      properties:
        status:
          type: string
          enum: [accepted]
          description: Заявка принята в фоновую обработку
        url:
          type: string
          format: uri
          description: URL, переданный в запросе

    ErrorResponse:
      type: object
      required:
        - status
        - message
      properties:
        status:
          type: string
          enum: [error]
        message:
          type: string

paths:
  /api/update:
    get:
      tags: [Indexing]
      summary: Обновить индекс документа по URL
      description: |
        Принимает URL документа через query-параметр `url` и выполняет синхронизацию индекса:
        - если документ новый, добавляет его в индекс;
        - если источник отвечает `404`, удаляет документ из индекса;
        - если источник возвращает редирект, удаляет старый URL и индексирует новый.

        Доступ без авторизации. Безопасность обеспечивается белым списком доменов:
        URL должен принадлежать домену из списка Source, который предварительно добавлен администратором.
      parameters:
        - name: url
          in: query
          required: true
          schema:
            type: string
            format: uri
          description: Прямая ссылка на документ
      responses:
        '200':
          description: Заявка принята в очередь фоновой обработки
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/IndexUpdateResponse'
        '400':
          description: Некорректный параметр url
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '403':
          description: Домен URL не разрешен (не входит в Source)
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Внутренняя ошибка индексации
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
```

---

## 3. Примеры

### 3.1 Индексация документа

**Request**

```http
GET /api/update?url=https%3A%2F%2Fdocs.example.org%2Freglament.pdf HTTP/1.1
Host: chat.vbudushee.ru
```

**Response (редирект обработан)**

```json
{
  "status": "accepted",
  "url": "https://docs.example.org/reglament.pdf"
}
```

---

## 4. Справка: встраивание виджета на сайт

Кроме API-методов выше, в проекте есть интеграция чат-виджета через JavaScript-встройку по адресу `https://chat.vbudushee.ru/widget`.

Базовый код встраивания:

```html
<script src="https://chat.vbudushee.ru/widget" defer></script>
<div id="vchat-chat"></div>
```

Рекомендации по встраиванию:
- разместите код перед закрывающим тегом `</body>`;
- `defer` обязателен, чтобы не блокировать загрузку страницы;
- контейнер `div#vchat-chat` обязателен: в него монтируется интерфейс виджета, это же способ регулировать размещение DOM объекта в структуре страницы сайта.
- встраивание выполняется единым скриптом `https://chat.vbudushee.ru/widget`.
