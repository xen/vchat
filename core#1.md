# Архитектурная спецификация проекта vbudushee (Core #1)

## 1. Введение и пояснительная записка
Данный документ описывает высокоуровневую архитектуру платформы «vbudushee» — системы для создания и управления ИИ чат-ботами с использованием технологии RAG (Retrieval-Augmented Generation). 

### Цели системы
- Предоставление внешним клиентам инструментов для «обучения» ИИ на собственных данных (документы, сайты).
- Интеграция обученных чат-ботов в веб-интерфейсы и сторонние ресурсы.
- Поддержка различных LLM-провайдеров (OpenAI, Anthropic) и локальных моделей.

### Архитектурные принципы
- **Гибридный фронтенд**: Использование Django-шаблонов (Jinja2) для SEO-значимых страниц и JS-модулей для динамических интерфейсов чата.
- **Асинхронность**: Тяжелые задачи (парсинг сайтов, генерация эмбеддингов) выносятся в Celery (брокер Redis).
- **Векторный поиск**: Использование расширения PGVector для PostgreSQL для эффективного поиска по семантическим сходствам.
- **Real-time**: Общение с ИИ через WebSocket для мгновенного отображения ответов (стриминг).

---

## 2. C4 Model: Уровень 1 (System Context)

Описывает взаимодействие системы «vbudushee» с внешним миром.

```mermaid
C4Context
    title System Context diagram for vbudushee AI Platform

    Person(customer, "Клиент/Администратор", "Пользователь, настраивающий чат-бота и загружающий данные.")
    Person(end_user, "Посетитель сайта", "Конечный пользователь, общающийся с ИИ чат-ботом.")

    System(v_system, "vbudushee Platform", "Позволяет создавать, настраивать и использовать ИИ чат-ботов на основе RAG.")

    System_Ext(llm_api, "LLM Providers", "OpenAI, Anthropic (API для генерации текста и эмбеддингов).")
    System_Ext(local_llm, "Local LLM", "Локальные модели для приватной обработки данных.")
    System_Ext(external_sites, "Внешние сайты", "Источники данных для краулера.")

    Rel(customer, v_system, "Настраивает ботов, загружает PDF/Docx", "HTTPS")
    Rel(end_user, v_system, "Задает вопросы чат-боту", "HTTPS/WSS")
    Rel(v_system, llm_api, "Отправляет запросы к моделям", "JSON/HTTPS")
    Rel(v_system, local_llm, "Взаимодействует с локальным инстансом", "gRPC/HTTP")
    Rel(v_system, external_sites, "Сканирует контент (Crawler)", "HTTPS")
```

---

## 3. C4 Model: Уровень 2 (Containers)

Детализирует внутренние компоненты платформы.

```mermaid
C4Container
    title Container diagram for vbudushee Platform

    Person(user, "Пользователь", "Клиент или посетитель.")

    Container_Boundary(c1, "Web Application") {
        Container(app, "Django Server", "Python/Django", "Обрабатывает бизнес-логику, управление проектами и API.")
        Container(ws_server, "WebSocket Handler", "Django Channels / Redis", "Обеспечивает real-time чат.")
        Container(frontend, "Hybrid Frontend", "Jinja2 / JS", "Интерфейс личного кабинета и виджет чата.")
    }

    Container_Boundary(c2, "Background Processing") {
        Container(celery, "Celery Workers", "Python", "Фоновая обработка: краулинг, эмбеддинги, индексация.")
        Container(redis, "Redis", "In-memory Store", "Брокер сообщений и кэш.")
    }

    Container_Boundary(c3, "Data Storage") {
        ContainerDb(db, "PostgreSQL + PGVector", "Relational DB", "Хранит метаданные, пользователей и векторные представления (chunks).")
    }

    Rel(user, frontend, "Использует", "HTTPS")
    Rel(frontend, app, "API запросы", "AJAX/JSON")
    Rel(frontend, ws_server, "Стриминг чата", "WSS")
    Rel(app, db, "Читает/Пишет", "SQL")
    Rel(app, redis, "Ставит задачи", "Protocol")
    Rel(ws_server, redis, "Pub/Sub", "Protocol")
    Rel(celery, redis, "Получает задачи", "Protocol")
    Rel(celery, db, "Сохраняет chunks и векторы", "SQL/PGVector")
```

---

## 4. Архитектурная схема процесса RAG (Flowchart)

Схема того, как данные попадают в систему и превращаются в ответы ИИ.

```mermaid
graph TD
    A[Загрузка данных: PDF, Docx, URL] --> B{Тип источника?}
    B -- Документ --> C[Извлечение текста]
    B -- Сайт --> D[Краулер/Парсер]
    
    C --> E[Разбиение на чанки - Chunking]
    D --> E
    
    E --> F[Генерация эмбеддингов - LLM API]
    F --> G[(PostgreSQL + PGVector)]
    
    subgraph "Процесс ответа (Inference)"
    H[Вопрос пользователя] --> I[Поиск похожих чанков в PGVector]
    I --> J[Сборка промпта: Контекст + Вопрос]
    J --> K[Запрос к LLM: OpenAI/Anthropic/Local]
    K --> L[Ответ пользователю через WebSocket]
    end
    
    G -.-> I
```

---

## 5. Дополнительные технические детали

1.  **Масштабируемость**: Использование Celery позволяет горизонтально масштабировать воркеры для обработки больших объемов документов.
2.  **Безопасность**: Доступ к данным ограничен в рамках проекта (Project isolation). Эмбеддинги привязаны к конкретному идентификатору проекта.
3.  **Гибкость моделей**: Архитектура предусматривает абстрактный слой для работы с провайдерами LLM, что позволяет переключаться между OpenAI и локальными решениями без переписывания ядра.
