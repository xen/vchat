# Руководство по эксплуатации проекта vchat

## 1. Для кого это руководство

Это руководство предназначено для администратора или инженера сопровождения,
которому нужно развернуть, настроить и поддерживать проект без глубокого
изучения исходного кода.

Документ написан как практическая инструкция: берите раздел, выполняйте шаги по
порядку, фиксируйте результат. Термины вроде `LDAP`, `DN`, `bind`, `Celery`,
`Redis`, `PostgreSQL`, `pgvector`, `GigaChat`, `RAG` оставлены в английском
написании, потому что так они обычно называются в настройках и логах.

> Человеческий комментарий: если вы не уверены, что именно менять, почти всегда
> нужно менять `local.yaml`, а не `vchat/config.yaml`. Файл `vchat/config.yaml`
> содержит базовые значения проекта, а `local.yaml` переопределяет их для
> конкретной среды.

## 2. Что входит в систему

Проект состоит из веб-приложения, фоновых задач и хранилищ данных.

| Компонент | За что отвечает |
|---|---|
| Web-приложение | Административный интерфейс, виджет чата, API, `/metrics` |
| PostgreSQL + `pgvector` | Пользователи, источники, страницы, чанки, embeddings |
| Redis | Очереди Celery, временные ключи, flash-сообщения, rate limit |
| Celery worker | Краулинг, переиндексация, служебные фоновые задачи |
| Embedder worker | Создание embeddings для страниц и сообщений |
| GigaChat или другой LLM provider | Генерация ответов пользователю |
| LDAP-сервер Заказчика | Корпоративная аутентификация пользователей |

```mermaid
flowchart LR
    U[Пользователь] --> W[Web-приложение]
    A[Администратор] --> W
    W --> P[(PostgreSQL + pgvector)]
    W --> R[(Redis)]
    W --> L[LDAP-сервер Заказчика]
    W --> G[GigaChat API]
    C[Celery worker] --> R
    C --> P
    E[Embedder worker] --> R
    E --> P
    C --> E
```

> Человеческий комментарий: если web работает, но ответы не используют новые
> документы, обычно проблема не в web, а в очереди Celery или embedder worker.

## 3. Где находятся основные файлы

| Файл или каталог | Назначение |
|---|---|
| `vchat/config.yaml` | Базовый конфиг проекта |
| `local.yaml` | Локальные и production-переопределения |
| `requirements/requirements.txt` | Production-зависимости |
| `requirements/dev.txt` | Dev-зависимости и инструменты |
| `migrations/` | Миграции базы данных Alembic |
| `jobs/` | Фоновые задачи, crawler, embedder |
| `vchat/views/` | Web routes, handlers, API |
| `deploy/` | Материалы для контейнерного или инфраструктурного запуска, если используются |
| `docs/` | Документация проекта |

## 4. Минимальные требования перед запуском

До запуска подготовьте:

- Python 3.11.
- PostgreSQL с базой проекта.
- Расширение PostgreSQL `pgvector`.
- Redis.
- Доступ к GigaChat или другому разрешенному LLM provider.
- Если используется корпоративный вход: доступ к LDAP-серверу Заказчика.
- Системные библиотеки для LDAP, например `libldap-dev` на Linux.

> Человеческий комментарий: если Python-пакет `bonsai` не ставится или падает
> при импорте, чаще всего не хватает системных LDAP-библиотек. Это исправляется
> на уровне окружения, а не добавлением fallback в код.

## 5. Первичная настройка проекта

### 5.1. Создайте виртуальное окружение и установите зависимости

Выполняйте команды из корня репозитория.

```bash
make setup
```

Эта команда:

- создает или использует `venv`;
- устанавливает зависимости из `requirements/dev.txt`;
- подготавливает модельные файлы через `entry.py --model`.

Если нужна production-установка зависимостей:

```bash
make deploy
```

> Человеческий комментарий: для обычной локальной проверки используйте
> `make setup`. Для сервера используйте процесс деплоя, согласованный в вашей
> инфраструктуре.

### 5.2. Подготовьте `local.yaml`

Создайте `local.yaml` в корне проекта. Начать можно с `local.example.yaml`.

Минимальный пример для локальной среды:

```yaml
public_url: "https://local.vchat.com"
mode: stage
sql_echo: false
cookie_secure: false
enable_https_middleware: false
cookie_domain: "local.vchat.com"

database_uri: "postgresql+asyncpg://xen@localhost:5432/vchat"
redis_uri: "redis://localhost:6379/30"
celery_redis_uri: "redis://localhost:6379/"
celery_broker_db: 31
celery_backend_db: 32

chat_provider: "gigachat"
chat_model: "GigaChat"
gigachat_api_key: "ВСТАВЬТЕ_BASIC_КЛЮЧ"
```

Для production-среды обязательно проверьте:

| Параметр | Что указать |
|---|---|
| `public_url` | Внешний HTTPS-адрес сервиса |
| `allowed_origins` | Домены, с которых разрешены запросы |
| `cookie_domain` | Домен cookie, например `.example.ru` |
| `cookie_secure` | `true`, если используется HTTPS |
| `enable_https_middleware` | `true`, если приложение должно принудительно использовать HTTPS |
| `database_uri` | Production PostgreSQL DSN |
| `redis_uri` | Redis для приложения |
| `celery_redis_uri` | База Redis, на которой живет Celery broker/backend |
| `gigachat_api_key` | Basic-ключ GigaChat |

> Человеческий комментарий: секреты, пароли и ключи нельзя коммитить в Git.
> Храните их в `local.yaml`, переменных окружения или в secret-хранилище вашей
> платформы.

## 6. Подготовка базы данных

### 6.1. Создайте базу PostgreSQL

Пример локальной команды:

```bash
createdb vchat
```

### 6.2. Включите `pgvector`

В базе должно быть доступно расширение `vector`.

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

### 6.3. Примените миграции

```bash
make db
```

Команда выполняет:

```bash
venv/bin/alembic upgrade head
```

### 6.4. Создайте первого локального пользователя, если basic-auth включен

```bash
make user
```

> Человеческий комментарий: если вы полностью переходите на LDAP, локального
> пользователя можно оставить только для аварийного административного доступа,
> если это разрешено политикой Заказчика.

## 7. Запуск сервисов

Для полноценной работы нужны минимум три процесса: web, Celery worker и embedder
worker.

### 7.1. Web-приложение

Локальный запуск:

```bash
make run
```

Прямой запуск без autoreload:

```bash
venv/bin/python -X dev entry.py
```

### 7.2. Celery worker

```bash
make celery
```

Этот worker обслуживает очередь `celery`: crawler, плановые задачи,
переиндексацию и служебные операции.

### 7.3. Embedder worker

```bash
make embedder
```

Этот процесс обслуживает очередь `embeddings` и создает embeddings для chunks.

### 7.4. Быстрая схема запуска

```mermaid
sequenceDiagram
    participant Admin as Администратор
    participant Web as Web
    participant Celery as Celery
    participant Embedder as Embedder
    participant DB as PostgreSQL
    participant Redis as Redis

    Admin->>DB: make db
    Admin->>Web: make run
    Admin->>Celery: make celery
    Admin->>Embedder: make embedder
    Web->>Redis: кладет задачи
    Celery->>DB: обновляет страницы
    Embedder->>DB: пишет embeddings
```

> Человеческий комментарий: запускайте web, Celery и embedder в разных
> терминалах или как разные systemd/container-процессы. Если запустить только
> web, интерфейс откроется, но индексация и embeddings работать полноценно не
> будут.

## 8. Настройка LDAP

### 8.1. Как LDAP используется в проекте

LDAP используется для входа пользователей в административный интерфейс.

Процесс входа:

1. Пользователь открывает `/login/ldap/`.
2. Вводит корпоративный email и пароль.
3. Приложение делает service bind к LDAP, если задан `ldap_bind_dn`.
4. Приложение ищет пользователя по `ldap_search_base` и `ldap_search_filter`.
5. Если задана группа `ldap_required_group_dn`, приложение проверяет `memberOf`.
6. Приложение делает user bind от имени найденного DN и введенного пароля.
7. При успешном входе локальная запись `User` создается автоматически.

```mermaid
flowchart TD
    F[Форма LDAP-входа] --> S[Service bind]
    S --> Q[Search user по email]
    Q --> G{Задана LDAP-группа?}
    G -- Нет --> B[User bind паролем пользователя]
    G -- Да --> M{memberOf содержит группу?}
    M -- Нет --> R[Отказ во входе]
    M -- Да --> B
    B --> U[Создание или поиск локального User]
    U --> OK[Сессия пользователя]
```

### 8.2. Какие LDAP-данные нужно запросить у Заказчика

| Что запросить | Пример |
|---|---|
| Адрес LDAP-сервера | `ldap://ldap.example.ru:389` или `ldaps://ldap.example.ru:636` |
| Нужно ли TLS/SSL | `ldap_use_ssl: true` для LDAPS |
| Service account DN | `CN=vchat-bind,OU=Service Accounts,DC=example,DC=ru` |
| Пароль service account | Передается как secret |
| Search base | `OU=Users,DC=example,DC=ru` |
| Атрибут email | Обычно `mail` или `userPrincipalName` |
| Атрибут имени | Обычно `displayName` |
| DN группы доступа | `CN=vchat-users,OU=Groups,DC=example,DC=ru` |
| Атрибут членства | Обычно `memberOf` |

> Человеческий комментарий: `DN` — это полный LDAP-путь объекта. Для группы не
> достаточно короткого имени `vchat-users`; нужен полный DN группы.

### 8.3. Рекомендуемый `local.yaml` для LDAP

Пример для Active Directory или похожего LDAP:

```yaml
auth_basic_enabled: false
auth_ldap_enabled: true

ldap_server: "ldaps://ldap.example.ru:636"
ldap_use_ssl: true

ldap_bind_dn: "CN=vchat-bind,OU=Service Accounts,DC=example,DC=ru"
ldap_bind_password: "ВСТАВЬТЕ_ПАРОЛЬ_SERVICE_ACCOUNT"

ldap_search_base: "OU=Users,DC=example,DC=ru"
ldap_search_filter: "(mail={email})"
ldap_attr_name: "displayName"

ldap_required_group_dn: "CN=vchat-users,OU=Groups,DC=example,DC=ru"
ldap_member_of_attr: "memberOf"
```

Если Заказчик использует вход по `userPrincipalName`, фильтр может быть таким:

```yaml
ldap_search_filter: "(userPrincipalName={email})"
```

Если нужно разрешить вход только активным пользователям AD и участникам группы,
обычно достаточно группы через `ldap_required_group_dn`. Если Заказчик просит
добавить дополнительные условия прямо в LDAP filter, используйте составной
фильтр:

```yaml
ldap_search_filter: "(&(mail={email})(objectClass=user))"
```

> Человеческий комментарий: не вставляйте пароль пользователя в конфиг. В конфиг
> помещается только пароль service account, если LDAP-сервер требует отдельную
> учетную запись для поиска.

### 8.4. Как работает ограничение по группе

Параметр `ldap_required_group_dn` включает проверку членства.

| Значение | Поведение |
|---|---|
| `ldap_required_group_dn: ""` | Войти может любой пользователь, найденный LDAP search и прошедший user bind |
| `ldap_required_group_dn: "CN=vchat-users,OU=Groups,DC=example,DC=ru"` | Войти может только пользователь, у которого `memberOf` содержит этот DN |

Сравнение DN выполняется без учета регистра и лишних пробелов вокруг частей DN.
Например, эти значения считаются одинаковыми:

```text
CN=VChat Users, OU=Groups, DC=example, DC=ru
cn=vchat users,ou=groups,dc=example,dc=ru
```

### 8.5. Что делать, если LDAP-группа не работает

Проверьте по порядку:

1. Пользователь точно входит в нужную группу.
2. У группы указан полный DN, а не короткое имя.
3. LDAP возвращает атрибут `memberOf` в результатах поиска.
4. Если атрибут называется иначе, поменяйте `ldap_member_of_attr`.
5. Service account имеет право читать `memberOf`.
6. `ldap_search_base` действительно охватывает пользователя.
7. `ldap_search_filter` находит ровно нужного пользователя.

> Человеческий комментарий: самая частая ошибка — указан `CN=vchat-users`
> вместо полного `CN=vchat-users,OU=Groups,DC=example,DC=ru`.

### 8.6. Когда можно оставить basic-auth

`auth_basic_enabled: true` оставляет локальный вход по email и паролю.

Обычно это допустимо только для:

- локальной разработки;
- аварийной учетной записи администратора;
- временного периода миграции на LDAP.

Для production, где Заказчик требует корпоративную аутентификацию, обычно
ставят:

```yaml
auth_basic_enabled: false
auth_ldap_enabled: true
```

## 9. Сессии пользователей

Сессия администратора хранится в encrypted cookie. Срок жизни задается
параметром:

```yaml
session_max_age_seconds: 2592000
```

Текущее значение `2592000` секунд равно 30 дням.

| Значение | Когда использовать |
|---|---|
| `28800` | 8 часов, строгий рабочий день |
| `43200` | 12 часов, рабочая смена |
| `86400` | 24 часа, умеренный баланс удобства и безопасности |
| `2592000` | 30 дней, удобно, но менее строго |

> Человеческий комментарий: сессии не вечные, но 30 дней для административного
> интерфейса может быть слишком долго. Если политика безопасности Заказчика
> строгая, поставьте 8 или 12 часов.

Важный нюанс: cookie-сессия проверяется на стороне приложения через подпись и
срок жизни. Если нужно принудительно завершить все текущие сессии, можно
ротировать `cookie_key`, но это разлогинит всех пользователей сразу.

## 10. Настройка LLM provider

Для production-сценария с GigaChat укажите:

```yaml
chat_provider: "gigachat"
chat_model: "GigaChat"
gigachat_api_key: "ВСТАВЬТЕ_BASIC_КЛЮЧ"
gigachat_base_url: "https://gigachat.devices.sberbank.ru/api/v1"
gigachat_oauth_url: "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
gigachat_scope: "GIGACHAT_API_PERS"
gigachat_verify_ssl_certs: true
```

Если OAuth GigaChat не работает:

| Признак | Что проверить |
|---|---|
| Ошибка `Missing GigaChat authorization key` | Задан ли `gigachat_api_key` |
| HTTP 401 или 403 | Корректен ли Basic-ключ |
| Timeout | Доступен ли `gigachat_oauth_url` из среды запуска |
| SSL error | Установлены ли доверенные сертификаты |

## 11. Источники и индексация

Материалы попадают в базу знаний через источники и файлы в административном
интерфейсе.

Поддерживаемые основные форматы извлечения:

| Формат | Статус |
|---|---|
| PDF | Извлекается через `pypdf` |
| DOCX | Извлекается через `python-docx` |
| PPTX | Извлекается через `python-pptx` |
| PPT | Не индексируется как legacy-формат |
| Excel | Не входит в целевой объем извлечения |

После добавления источника или файла проверьте:

1. Страница или файл появились в админке.
2. Для документа создан raw content.
3. Celery worker обработал задачу.
4. Embedder worker создал chunks и embeddings.
5. В чате появились ответы с цитированием источников.

> Человеческий комментарий: если документ виден в админке, но не участвует в
> ответах, почти всегда нужно смотреть статус chunks и очередь `embeddings`.

## 12. Очереди Celery и Redis

В проекте используются две основные очереди:

| Очередь | Назначение |
|---|---|
| `celery` | Краулинг, обновление источников, служебные задачи |
| `embeddings` | Построение embeddings |

Параметры Redis по умолчанию:

```yaml
redis_uri: redis://localhost:6379/30
celery_redis_uri: redis://localhost:6379/
celery_broker_db: 31
celery_backend_db: 32
```

Для диагностики очередей локально:

```bash
redis-cli -n 31 LLEN celery
redis-cli -n 31 LLEN embeddings
```

> Человеческий комментарий: смотрите очередь в `celery_broker_db`, а не в
> `redis_uri`. `redis_uri` используется приложением, а Celery broker живет в
> отдельной Redis DB.

## 13. Наблюдаемость

Приложение отдает Prometheus-метрики:

```text
/metrics
```

В репозитории есть правила алертов:

```text
deploy/prometheus_alerts.yml
```

Минимально полезные проверки:

| Что проверить | Почему важно |
|---|---|
| Web отвечает | Пользователи могут открыть интерфейс |
| `/metrics` открывается | Prometheus сможет собрать метрики |
| Очередь `celery` не растет бесконечно | Worker успевает обрабатывать задачи |
| Очередь `embeddings` не растет бесконечно | Embedder успевает строить embeddings |
| В логах нет повторяющихся LDAP/GigaChat ошибок | Аутентификация и генерация работают |

## 14. Обновление проекта

Стандартный порядок обновления:

1. Остановить процессы приложения по процедуре вашей инфраструктуры.
2. Обновить код.
3. Установить зависимости.
4. Применить миграции.
5. Собрать frontend, если он менялся.
6. Запустить web, Celery и embedder.
7. Проверить вход, `/metrics`, индексацию и тестовый вопрос в чате.

Команды:

```bash
make deploy
make frontend
```

Если нужно только применить миграции:

```bash
make db
```

> Человеческий комментарий: не запускайте миграции “на всякий случай” из
> нескольких мест одновременно. Миграции должны выполняться один раз на
> окружение в рамках контролируемого деплоя.

## 15. Проверочный чеклист после запуска

| Шаг | Ожидаемый результат |
|---|---|
| Открыть страницу входа | Видна форма входа |
| Войти через LDAP | Пользователь попадает в админку |
| Проверить пользователя в списке | У пользователя стоит `is_ldap` |
| Открыть `/metrics` | Возвращается Prometheus text format |
| Добавить тестовый источник или файл | Источник сохраняется |
| Запустить или дождаться индексации | Создаются chunks и embeddings |
| Задать вопрос в виджете | Ответ содержит релевантный текст и источники |
| Проверить логи | Нет повторяющихся ошибок LDAP, Redis, DB, GigaChat |

## 16. Типовые проблемы

| Симптом | Что делать |
|---|---|
| LDAP-вход всегда отклоняется | Проверить `ldap_search_base`, `ldap_search_filter`, service account и пароль |
| LDAP-вход работает без группы, но не работает с группой | Проверить полный DN группы и наличие `memberOf` |
| Пользователь входит, но документы не индексируются | Проверить Celery worker и очередь `celery` |
| Chunks есть, но embeddings не появляются | Проверить embedder worker и очередь `embeddings` |
| GigaChat не отвечает | Проверить `gigachat_api_key`, OAuth URL, SSL и сетевой доступ |
| Сессия живет слишком долго | Уменьшить `session_max_age_seconds` |
| Все пользователи должны выйти из системы | Ротировать `cookie_key` и перезапустить web |

## 17. Что не надо делать без согласования

- Не коммитьте `local.yaml` с секретами.
- Не меняйте `vchat/config.yaml` для разовых production-секретов.
- Не отключайте SSL-проверку GigaChat без письменного решения.
- Не включайте basic-auth в production, если политика Заказчика требует только LDAP.
- Не запускайте несколько несовместимых Celery broker DB для одного окружения.
- Не чистите Redis DB целиком без понимания, какие очереди и временные ключи там лежат.
- Не ротируйте `cookie_key` в рабочее время без предупреждения пользователей.

## 18. Минимальный production-пример `local.yaml`

Ниже пример, который нужно адаптировать под реальную инфраструктуру.

```yaml
mode: production
public_url: "https://vchat.example.ru"
allowed_origins:
  - "https://vchat.example.ru"

cookie_domain: ".example.ru"
cookie_secure: true
enable_https_middleware: true
session_max_age_seconds: 43200

database_uri: "postgresql+asyncpg://vchat:CHANGE_ME@postgres.example.ru:5432/vchat"
redis_uri: "redis://redis.example.ru:6379/30"
celery_redis_uri: "redis://redis.example.ru:6379/"
celery_broker_db: 31
celery_backend_db: 32

chat_provider: "gigachat"
chat_model: "GigaChat"
gigachat_api_key: "CHANGE_ME"
gigachat_verify_ssl_certs: true

auth_basic_enabled: false
auth_ldap_enabled: true
ldap_server: "ldaps://ldap.example.ru:636"
ldap_use_ssl: true
ldap_bind_dn: "CN=vchat-bind,OU=Service Accounts,DC=example,DC=ru"
ldap_bind_password: "CHANGE_ME"
ldap_search_base: "OU=Users,DC=example,DC=ru"
ldap_search_filter: "(mail={email})"
ldap_attr_name: "displayName"
ldap_required_group_dn: "CN=vchat-users,OU=Groups,DC=example,DC=ru"
ldap_member_of_attr: "memberOf"
```

> Человеческий комментарий: этот пример нельзя копировать в production без
> замены доменов, паролей и DN. Он нужен как форма, которую удобно заполнить
> вместе с инфраструктурной командой Заказчика.
