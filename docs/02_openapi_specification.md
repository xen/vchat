# OpenAPI спецификация API VChat

## 1. Назначение

Документ описывает публичные API-методы проекта, необходимые для:

- синхронизации/индексации документов по URL;
- отправки пользовательского обращения в поддержку из виджета.

Все остальные API-методы из этой спецификации удалены как неактуальные.

## 2. OpenAPI 3.0 (YAML)

```yaml
openapi: 3.0.3
info:
  title: VChat Public API
  version: 1.0.0
  description: |
    Минимальный публичный API:
    1) /api/update — обновление индекса по URL документа;
    2) /api/support/request — создание обращения пользователя из виджета.
servers:
  - url: https://chat.vbudushee.ru
    description: Production

tags:
  - name: Indexing
    description: Синхронизация документов в индексе
  - name: Support
    description: Обращения пользователей из виджета

components:
  securitySchemes:
    CsrfHeader:
      type: apiKey
      in: header
      name: X-CSRFToken

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

    SupportRequestCreate:
      type: object
      required:
        - name
        - email
        - phone
        - body
      properties:
        name:
          type: string
          maxLength: 255
          description: Имя пользователя
        email:
          type: string
          format: email
          maxLength: 255
        phone:
          type: string
          maxLength: 64
        body:
          type: string
          maxLength: 5000
          description: Текст обращения

    SupportRequestCreated:
      type: object
      required:
        - status
        - request_id
      properties:
        status:
          type: string
          enum: [ok]
        request_id:
          type: integer
          description: Идентификатор созданного объекта Request

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

  /api/support/request:
    post:
      tags: [Support]
      summary: Создать обращение пользователя в поддержку
      description: |
        Создает объект `vchat/models/support.py::Request`.

        Требования:
        - запрос должен содержать CSRF токен в заголовке `X-CSRFToken`;
        - `chat_id` извлекается на сервере из CSRF токена (токен подписан через itsdangerous);
        - дополнительно сохраняются IP-адрес и User-Agent пользователя.
      security:
        - CsrfHeader: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/SupportRequestCreate'
      responses:
        '201':
          description: Обращение создано
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/SupportRequestCreated'
        '400':
          description: Ошибка валидации данных
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '403':
          description: Некорректный/просроченный CSRF токен
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '500':
          description: Внутренняя ошибка
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

### 3.2 Создание обращения

**Request**

```http
POST /api/support/request HTTP/1.1
Host: chat.vbudushee.ru
Content-Type: application/json
X-CSRFToken: <csrf_token_from_widget>

{
  "name": "Иван Петров",
  "email": "ivan@example.com",
  "phone": "+79991234567",
  "body": "Подскажите, где посмотреть актуальные правила подачи заявки?"
}
```

**Response**

```json
{
  "status": "ok",
  "request_id": 1024
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
