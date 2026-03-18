# OpenAPI Спецификация API АС «ВБУДУЩЕЕ» (Core #2)

## 1. Пояснительная записка
Данная спецификация описывает программные интерфейсы (API) сервиса «ИИ-агент-чат-бот», развернутого в контуре АС «ВБУДУЩЕЕ».
API спроектирован для обеспечения работы RAG-системы с использованием GigaChat API.

### Основные принципы
- **RESTful API**: Для управления документами, сессиями и настройками индексации.
- **Streaming (Server-Sent Events / WebSockets)**: Для потоковой выдачи ответов от LLM в реальном времени.
- **Интеграция с Битрикс**: Бесшовное взаимодействие фронт-виджета с бэкенд-сервисом.
- **Безопасность**: Обязательная валидация промптов (Guardrails), логирование personalID и защита от инъекций.

---

## 2. OpenAPI 3.0 Спецификация (YAML)

```yaml
openapi: 3.0.3
info:
  title: АС «ВБУДУЩЕЕ» AI Agent API
  description: |
    API сервиса интеллектуального чат-бота.
    Обеспечивает поиск по базе знаний Фонда и генерацию ответов через GigaChat.
  version: 1.0.0
servers:
  - url: /api/v1
    description: Продакшн-сервер (инфраструктура Фонда)

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
    ChatRequest:
      type: object
      required: [message]
      properties:
        message:
          type: string
          description: "Пользовательский промпт"
          maxLength: 1000
        session_id:
          type: string
          description: "Идентификатор сессии для сохранения памяти"

    ChatResponse:
      type: object
      properties:
        answer:
          type: string
          description: "Текст ответа от GigaChat"
        sources:
          type: array
          items:
            $ref: '#/components/schemas/SourceReference'

    SourceReference:
      type: object
      properties:
        title:
          type: string
          description: "Название документа-источника"
        url:
          type: string
          description: "Ссылка на документ в хранилище"

    ContactForm:
      type: object
      required: [email, question, consent]
      properties:
        email:
          type: string
          format: email
        question:
          type: string
        consent:
          type: boolean
          description: "Согласие на обработку ПДн"

paths:
  # --- CHAT INTERACTION ---
  /chat/ask:
    post:
      summary: Отправить вопрос чат-боту
      description: |
        Выполняет RAG-поиск по векторной БД и генерирует ответ через GigaChat API.
        Включает механизмы защиты (Guardrails).
      security:
        - SessionAuth: []
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/ChatRequest'
      responses:
        '200':
          description: Ответ сформирован
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ChatResponse'
        '429':
          description: Превышен лимит запросов (Rate Limit)

  # --- CONTACT FORM (ГИБРИДНАЯ ФОРМА) ---
  /support/contact:
    post:
      summary: Форма «Написать человеку»
      description: Отправляет обращение на служебную почту Фонда.
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/ContactForm'
      responses:
        '200':
          description: Сообщение успешно отправлено

  # --- INDEXING & SYNC ---
  /admin/sync/start:
    post:
      summary: Запуск синхронизации с хранилищем
      description: |
        Сканирует хранилище материалов (PDF, Word, Excel).
        Выполняет извлечение текста, разбиение на чанки и переиндексацию.
      security:
        - SessionAuth: []
      responses:
        '202':
          description: Синхронизация запущена в фоновом режиме

  # --- MONITORING ---
  /metrics:
    get:
      summary: Метрики в формате Prometheus
      description: |
        Экспорт ключевых показателей: время ответа, расход токенов GigaChat,
        доля ошибок, скорость индексации.
      responses:
        '200':
          description: Текст в формате Prometheus exposition
```

---

## 3. Примеры использования

### 1. Запрос к чат-боту (RAG)
**Request:**

```http
POST /api/v1/chat/ask HTTP/1.1
Content-Type: application/json
X-CSRFToken: <token>

{
  "message": "Какие цифровые навыки развивает программа Фонда?",
  "session_id": "sess_998877"
}
```

**Response:**

```json
{
  "answer": "Программа развивает компетенции в области ИИ и работы с данными...",
  "sources": [
    {
      "title": "Паспорт программы ЦНК.pdf",
      "url": "https://vbudushee.ru/storage/docs/p1.pdf"
    }
  ]
}
```

### 2. Отправка формы обратной связи
**Request:**

```http
POST /api/v1/support/contact HTTP/1.1
Content-Type: application/json

{
  "email": "user@example.com",
  "question": "Не могу найти документ о грантах",
  "consent": true
}
```

---

## 4. Требования безопасности API

- **Маскирование**: В логах «запрос-ответ» персональные данные пользователей должны быть маскированы.
- **Токены**: Все ключи доступа к GigaChat API хранятся в защищенной системе управления секретами (не в коде).
- **Валидация**: Каждое обращение проходит проверку на соответствие теме (Классификатор релевантности).
- **Трассировка**: Каждому запросу присваивается уникальный идентификатор для сквозного мониторинга.
