# OpenAPI Спецификация API проекта vbudushee (Core #2)

## 1. Пояснительная записка
Данная спецификация описывает программные интерфейсы (API) платформы «vbudushee». Проект использует гибридный подход:
- **HTMX/Action API**: Большинство операций (создание проектов, запуск индексации, удаление) реализовано через универсальный эндпоинт `/actions/project/{action}/{item_id}`.
- **Data API**: JSON-эндпоинты для получения списков документов и статистики.
- **Real-time API**: WebSocket-интерфейсы для потоковой передачи ответов ИИ.
- **File API (TUS)**: Протокол для надежной загрузки больших файлов.

### Аутентификация и Безопасность
- **Сессии**: Используются cookie-сессии.
- **CSRF**: Для всех POST/PUT/DELETE запросов обязателен заголовок `X-CSRFToken`. Токен привязан к ID пользователя.
- **Авторизация**: Доступ к проектам ограничен владельцами (проверяется через таблицу `ProjectUser`).

---

## 2. OpenAPI 3.0 Спецификация (YAML)

```yaml
openapi: 3.0.3
info:
  title: vbudushee AI Platform API
  description: API для управления проектами, источниками данных и чат-ботами.
  version: 1.0.0
servers:
  - url: /
    description: Текущий сервер

components:
  securitySchemes:
    SessionAuth:
      type: apiKey
      in: cookie
      name: session_id
    CSRFToken:
      type: apiKey
      in: header
      name: X-CSRFToken

  schemas:
    Project:
      type: object
      properties:
        id:
          type: string
          example: "p123abc"
        title:
          type: string
        description:
          type: string
        provider:
          type: string
          description: "ID провайдера LLM (openai, anthropic, local)"
        model:
          type: string

    Document:
      type: object
      properties:
        id:
          type: string
        title:
          type: string
        type:
          type: string
          enum: [pdf, docx, url, text]
        status:
          type: string
          enum: [new, processing, indexed, error]

paths:
  # --- PROJECTS ---
  /project/:
    get:
      summary: Список проектов пользователя
      responses:
        '200':
          description: HTML-страница или JSON список

  /project/{project_id}/documents/json:
    get:
      summary: Список документов проекта в формате JSON
      parameters:
        - name: project_id
          in: path
          required: true
          schema:
            type: string
      responses:
        '200':
          description: Массив документов
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: '#/components/schemas/Document'

  # --- ACTIONS ---
  /actions/project/{action}/{item_id}:
    post:
      summary: Универсальный обработчик действий
      description: |
        Поддерживаемые действия (`action`):
        - `create_project` (item_id: any)
        - `update_ai_settings` (item_id: project_id)
        - `crawl_source` (item_id: source_id)
        - `refresh_source_index` (item_id: source_id)
        - `crawl_all` (item_id: project_id)
        - `index_project` (item_id: project_id)
      parameters:
        - name: action
          in: path
          required: true
          schema:
            type: string
        - name: item_id
          in: path
          required: true
          schema:
            type: string
      responses:
        '200':
          description: Успешное выполнение (часто возвращает HTML для HTMX)
        '403':
          description: Ошибка CSRF или доступа

  # --- CHAT & WEBSOCKETS ---
  /ws/chat/{payload}:
    get:
      summary: WebSocket для общения с чат-ботом
      description: |
        Payload содержит закодированные данные сессии и ID чата.
        Обеспечивает стриминг ответов от LLM.
      parameters:
        - name: payload
          in: path
          required: true
          schema:
            type: string
      responses:
        '101':
          description: Переключение на протокол WebSocket

  # --- UPLOADS ---
  /uploads/{project_id}/:
    post:
      summary: Инициализация загрузки файла (протокол TUS)
      parameters:
        - name: project_id
          in: path
          required: true
          schema:
            type: string
      responses:
        '201':
          description: Создан ресурс для загрузки
          headers:
            Location:
              schema:
                type: string

---

## 3. Примеры использования

### Создание проекта
**Request:**
```http
POST /actions/project/create_project/new HTTP/1.1
Content-Type: application/x-www-form-urlencoded
X-CSRFToken: <your_token>

title=Мой новый бот&description=Бот для техподдержки
```

### Запуск индексации всех источников
**Request:**
```http
POST /actions/project/crawl_all/p123abc HTTP/1.1
X-CSRFToken: <your_token>
```

### Получение документов (JSON)
**Request:**
```http
GET /project/p123abc/documents/json HTTP/1.1
```

**Response:**
```json
[
  {
    "id": "doc456",
    "title": "Инструкция.pdf",
    "type": "pdf",
    "status": "indexed"
  }
]
```
