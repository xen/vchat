# Руководство по эксплуатации проекта vchat

## 1. Для кого это руководство

Это руководство предназначено для администратора или инженера сопровождения,
которому нужно развернуть, настроить и поддерживать проект без глубокого
изучения исходного кода.

Документ написан как практическая инструкция: берите раздел, выполняйте шаги по
порядку, фиксируйте результат. Термины вроде `LDAP`, `DN`, `bind`, `Celery`,
`Redis`, `PostgreSQL`, `pgvector`, `GigaChat`, `RAG` оставлены в английском
написании, потому что так они обычно называются в настройках и логах.

> Комментарий: если вы не уверены, что именно менять, почти всегда
> нужно менять `local.yaml`, а не `vchat/config.yaml`. Файл `vchat/config.yaml`
> содержит базовые значения проекта, а `local.yaml` переопределяет их для
> конкретной среды.

## 2. Что входит в систему

Проект состоит из веб-приложения, фоновых задач и хранилищ данных.

| Компонент                        | За что отвечает                                              |
| -------------------------------- | ------------------------------------------------------------ |
| Web-приложение                   | Административный интерфейс, виджет чата, API, `/metrics`     |
| PostgreSQL + `pgvector`          | Пользователи, источники, страницы, чанки, embeddings         |
| Redis                            | Очереди Celery, временные ключи, flash-сообщения, rate limit |
| Celery worker                    | Краулинг, переиндексация, служебные фоновые задачи           |
| Embedder worker                  | Создание embeddings для страниц и сообщений                  |
| GigaChat или другой LLM provider | Генерация ответов пользователю                               |
| LDAP-сервер Заказчика            | Корпоративная аутентификация пользователей                   |

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

> Комментарий: если web работает, но ответы не используют новые
> документы, обычно проблема не в web, а в очереди Celery или embedder worker.

## 3. Где находятся основные файлы

| Файл или каталог                | Назначение                                                                   |
| ------------------------------- | ---------------------------------------------------------------------------- |
| `vchat/config.yaml`             | Базовый конфиг проекта                                                       |
| `local.yaml`                    | Локальные и production-переопределения                                       |
| `requirements/requirements.txt` | Production-зависимости                                                       |
| `requirements/dev.txt`          | Dev-зависимости и инструменты                                                |
| `migrations/`                   | Миграции базы данных Alembic                                                 |
| `jobs/`                         | Фоновые задачи, crawler, embedder                                            |
| `vchat/views/`                  | Web routes, handlers, API                                                    |
| `deploy/`                       | Материалы для контейнерного или инфраструктурного запуска, если используются |
| `docs/`                         | Документация проекта                                                         |

## 4. Минимальные требования перед запуском

До запуска подготовьте:

- Python 3.11.
- PostgreSQL с базой проекта.
- Расширение PostgreSQL `pgvector`.
- Redis.
- Доступ к GigaChat или другому разрешенному LLM provider.
- Если используется корпоративный вход: доступ к LDAP-серверу Заказчика.
- Системные библиотеки для LDAP, например `libldap-dev` на Linux.

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

> Комментарий: для обычной локальной проверки используйте
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

| Параметр                  | Что указать                                                     |
| ------------------------- | --------------------------------------------------------------- |
| `public_url`              | Внешний HTTPS-адрес сервиса                                     |
| `allowed_origins`         | Домены, с которых разрешены запросы                             |
| `cookie_domain`           | Домен cookie, например `.example.ru`                            |
| `cookie_secure`           | `true`, если используется HTTPS                                 |
| `enable_https_middleware` | `true`, если приложение должно принудительно использовать HTTPS |
| `database_uri`            | Production PostgreSQL DSN                                       |
| `redis_uri`               | Redis для приложения                                            |
| `celery_redis_uri`        | База Redis, на которой живет Celery broker/backend              |
| `gigachat_api_key`        | Basic-ключ GigaChat                                             |

> Комментарий: секреты, пароли и ключи нельзя коммитить в Git.
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

> Комментарий: если вы полностью переходите на LDAP, локального
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

> Комментарий: запускайте web, Celery и embedder в разных
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

| Что запросить          | Пример                                                         |
| ---------------------- | -------------------------------------------------------------- |
| Адрес LDAP-сервера     | `ldap://ldap.example.ru:389` или `ldaps://ldap.example.ru:636` |
| Нужно ли TLS/SSL       | `ldap_use_ssl: true` для LDAPS                                 |
| Service account DN     | `CN=vchat-bind,OU=Service Accounts,DC=example,DC=ru`           |
| Пароль service account | Передается как secret                                          |
| Search base            | `OU=Users,DC=example,DC=ru`                                    |
| Атрибут email          | Обычно `mail` или `userPrincipalName`                          |
| Атрибут имени          | Обычно `displayName`                                           |
| DN группы доступа      | `CN=vchat-users,OU=Groups,DC=example,DC=ru`                    |
| Атрибут членства       | Обычно `memberOf`                                              |

> Комментарий: `DN` — это полный LDAP-путь объекта. Для группы не
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

> Комментарий: не вставляйте пароль пользователя в конфиг. В конфиг
> помещается только пароль service account, если LDAP-сервер требует отдельную
> учетную запись для поиска.

### 8.4. Как работает ограничение по группе

Параметр `ldap_required_group_dn` включает проверку членства.

| Значение                                                              | Поведение                                                                   |
| --------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `ldap_required_group_dn: ""`                                          | Войти может любой пользователь, найденный LDAP search и прошедший user bind |
| `ldap_required_group_dn: "CN=vchat-users,OU=Groups,DC=example,DC=ru"` | Войти может только пользователь, у которого `memberOf` содержит этот DN     |

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

> Комментарий: самая частая ошибка — указан `CN=vchat-users`
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
auth_session_time: 0
```

Текущее значение `2592000` секунд равно 30 дням.
`session_max_age_seconds` задает максимальный срок жизни encrypted cookie.
`auth_session_time` задает дополнительный срок жизни пользовательской сессии
от момента успешного входа. Если `auth_session_time` больше `0`, приложение
записывает время логина в сессию и инвалидирует ее после указанного количества
секунд. Значение `0` отключает эту дополнительную проверку.

| Значение  | Когда использовать                                |
| --------- | ------------------------------------------------- |
| `28800`   | 8 часов, строгий рабочий день                     |
| `43200`   | 12 часов, рабочая смена                           |
| `86400`   | 24 часа, умеренный баланс удобства и безопасности |
| `2592000` | 30 дней, удобно, но менее строго                  |

> Комментарий: сессии не вечные, но 30 дней для административного
> интерфейса может быть слишком долго. Если политика безопасности Заказчика
> строгая, поставьте 8 или 12 часов. Для LDAP-инсталляций можно оставить
> cookie TTL длиннее, но выставить `auth_session_time`, чтобы пользователи
> регулярно проходили повторный вход и проверку LDAP-политик.

Важный нюанс: cookie-сессия проверяется на стороне приложения через подпись и
срок жизни. Если нужно принудительно завершить все текущие сессии, можно
ротировать `cookie_key`, но это разлогинит всех пользователей сразу.

### 9.1. Ограничения размера документов

Размер загружаемых и скачиваемых документов ограничивается в байтах:

```yaml
max_upload_size: 5242880
raw_content_max_bytes: 10485760
```

`max_upload_size` применяется к пользовательским загрузкам файлов.
`raw_content_max_bytes` применяется к сырому содержимому документов, которые
приложение скачивает по URL через API обновления, crawler и sitemap discovery.
Значение `10485760` равно `10 * 1024 * 1024`, то есть 10 MiB. Если удаленный
сервер заранее прислал больший `Content-Length` или тело ответа превысило лимит
во время чтения, документ отклоняется и не индексируется.

Для sitemap дополнительно действует стандартное ограничение: не больше `50000`
entries в одном `urlset` или `sitemapindex`.

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

| Признак                                     | Что проверить                                     |
| ------------------------------------------- | ------------------------------------------------- |
| Ошибка `Missing GigaChat authorization key` | Задан ли `gigachat_api_key`                       |
| HTTP 401 или 403                            | Корректен ли Basic-ключ                           |
| Timeout                                     | Доступен ли `gigachat_oauth_url` из среды запуска |
| SSL error                                   | Установлены ли доверенные сертификаты             |

## 11. Источники и индексация

Материалы попадают в базу знаний через источники и файлы в административном
интерфейсе.

Поддерживаемые основные форматы извлечения:

| Формат | Статус                               |
| ------ | ------------------------------------ |
| PDF    | Извлекается через `pypdf`            |
| DOCX   | Извлекается через `python-docx`      |
| PPTX   | Извлекается через `python-pptx`      |
| PPT    | Не индексируется как legacy-формат   |
| Excel  | Не входит в целевой объем извлечения |

После добавления источника или файла проверьте:

1. Страница или файл появились в админке.
2. Для документа создан raw content.
3. Celery worker обработал задачу.
4. Embedder worker создал chunks и embeddings.
5. В чате появились ответы с цитированием источников.

> Комментарий: если документ виден в админке, но не участвует в
> ответах, почти всегда нужно смотреть статус chunks и очередь `embeddings`.

## 12. Очереди Celery и Redis

В проекте используются две основные очереди:

| Очередь      | Назначение                                        |
| ------------ | ------------------------------------------------- |
| `celery`     | Краулинг, обновление источников, служебные задачи |
| `embeddings` | Построение embeddings                             |

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

> Комментарий: смотрите очередь в `celery_broker_db`, а не в
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

| Что проверить                                  | Почему важно                         |
| ---------------------------------------------- | ------------------------------------ |
| Web отвечает                                   | Пользователи могут открыть интерфейс |
| `/metrics` открывается                         | Prometheus сможет собрать метрики    |
| Очередь `celery` не растет бесконечно          | Worker успевает обрабатывать задачи  |
| Очередь `embeddings` не растет бесконечно      | Embedder успевает строить embeddings |
| В логах нет повторяющихся LDAP/GigaChat ошибок | Аутентификация и генерация работают  |

## 14. Развертывание в Kubernetes

Kubernetes-артефакты находятся в каталоге `deploy/`. Они не заменяют
инфраструктурные сервисы Заказчика: кластер, ingress controller, PostgreSQL,
Redis, registry, Prometheus/Grafana и secret management должны быть подготовлены
инфраструктурной командой.

### 14.1. Что есть в `deploy/`

| Путь                           | Назначение                                                        |
| ------------------------------ | ----------------------------------------------------------------- |
| `deploy/Dockerfile`            | Production image для web, Celery, Celery beat и embedder          |
| `deploy/compose.yaml`          | Локальный smoke test контейнерного запуска                        |
| `deploy/k8s/base/`             | Базовые Kubernetes manifests                                      |
| `deploy/k8s/keda/`             | Optional overlay для масштабирования worker по Redis queue length |
| `deploy/k8s/monitoring/`       | Optional `ServiceMonitor` для Prometheus Operator                 |
| `deploy/prometheus_alerts.yml` | Prometheus alert rules                                            |
| `deploy/README.md`             | Краткая техническая инструкция по deploy-артефактам               |

Базовый Kubernetes-набор создает:

- `Deployment/vchat-web`;
- `Deployment/vchat-celery`;
- `Deployment/vchat-celery-beat`;
- `Deployment/vchat-embedder`;
- `Job/vchat-migrate`;
- `Service/vchat-web`;
- `Service/vchat-metrics`;
- `Ingress/vchat`;
- `HorizontalPodAutoscaler/vchat-web`;
- `NetworkPolicy/vchat-web-ingress`;
- `ConfigMap/vchat-config`.

> Комментарий: `celery-beat` вынесен в отдельный deployment с
> `replicas: 1`. Его нельзя горизонтально масштабировать как обычный worker,
> иначе плановые задачи начнут ставиться в очередь несколько раз.

### 14.2. Сборка Docker image

Сборка выполняется из корня репозитория:

```bash
docker build -f deploy/Dockerfile -t registry.example.com/vchat:<TAG> .
```

Если в runtime не используется volume для моделей, запекайте модели в image:

```bash
docker build \
  -f deploy/Dockerfile \
  --build-arg DOWNLOAD_MODELS=true \
  -t registry.example.com/vchat:<TAG> .
```

Образ создает `/app/venv`, запускается от UID/GID `10001` и не требует
записи в директорию приложения. Kubernetes manifests монтируют writable
`emptyDir` только в `/tmp`.

Перед публикацией image в production registry рекомендуется:

```bash
trivy image registry.example.com/vchat:<TAG>
```

Для production используйте immutable tags или digest pinning:

```text
registry.example.com/vchat@sha256:...
```

> Комментарий: тег `latest` удобен для примеров, но плох для
> production-развертывания. Оператор должен понимать, какая именно сборка
> запущена в кластере.

### 14.3. Как передаются настройки

Приложение читает `vchat/config.yaml`, а затем переопределяет значения из
`/app/local.yaml`. В Kubernetes этот файл монтируется из `ConfigMap`:

```text
deploy/k8s/base/configmap.yaml -> /app/local.yaml
```

Секреты передаются через `Secret/vchat-secret` и подставляются в `local.yaml`
через существующий синтаксис `!env "$NAME"`. Также приложение применяет env
override после чтения `local.yaml`, поэтому переменные окружения могут
переопределять одноименные ключи конфигурации напрямую.

В `mode: production` приложение не стартует, если `secret_key`, `cookie_key`
или `vchat_secret` остались дефолтными, пустыми или равны `change-me`.

Минимальный набор secret-переменных:

| Secret key         | За что отвечает                                     |
| ------------------ | --------------------------------------------------- |
| `DATABASE_URI`     | DSN PostgreSQL в формате `postgresql+asyncpg://...` |
| `OPENAI_API_KEY`   | Ключ OpenAI, если используется OpenAI provider      |
| `GIGACHAT_API_KEY` | Basic-ключ GigaChat                                 |
| `SECRET_KEY` | Ключ подписи токенов приложения                     |
| `COOKIE_KEY` | Ключ encrypted cookie session                       |
| `VCHAT_SECRET` | Ключ интеграции проекта vchat                   |

Создание secret вручную:

```bash
kubectl -n vchat create secret generic vchat-secret \
  --from-literal=DATABASE_URI='postgresql+asyncpg://...' \
  --from-literal=OPENAI_API_KEY='...' \
  --from-literal=GIGACHAT_API_KEY='...' \
  --from-literal=SECRET_KEY='...' \
  --from-literal=COOKIE_KEY='...' \
  --from-literal=VCHAT_SECRET='...'
```

Если в кластере используется External Secrets, Sealed Secrets или Vault,
создайте объект, который в итоге даст такой же `Secret/vchat-secret`.

### 14.4. Ключи `local.yaml`, важные для Kubernetes

| Ключ                                    | За что отвечает                       | На что обратить внимание                                                    |
| --------------------------------------- | ------------------------------------- | --------------------------------------------------------------------------- |
| `mode`                                  | Режим приложения                      | Для production ставьте `production`                                         |
| `public_url`                            | Внешний URL сервиса                   | Должен совпадать с Ingress host и HTTPS-схемой                              |
| `public_cdn`                            | Базовый URL для статических ресурсов  | Обычно совпадает с `public_url`, если нет CDN                               |
| `allowed_origins`                       | Разрешенные CORS origins              | Добавьте только реальные домены портала/админки                             |
| `cookie_domain`                         | Домен cookie                          | Для поддоменов обычно `.example.ru`                                         |
| `cookie_secure`                         | Secure-флаг cookie                    | В production должен быть `true`                                             |
| `enable_https_middleware`               | Учет HTTPS за reverse proxy           | Должен быть `true`, если Ingress terminates TLS                             |
| `session_max_age_seconds`               | TTL админской сессии                  | Согласуйте с политикой безопасности                                         |
| `auth_session_time`                     | TTL от момента успешного логина       | `0` отключает; при значении больше `0` старые сессии без `login_at` истекут |
| `max_upload_size`                       | Лимит пользовательской загрузки       | Значение в байтах; `5242880` равно 5 MiB                                    |
| `raw_content_max_bytes`                 | Лимит скачиваемого документа          | Значение в байтах; `10485760` равно 10 MiB                                  |
| `database_uri`                          | PostgreSQL DSN                        | Передавайте через secret, не через ConfigMap                                |
| `redis_uri`                             | Redis DB приложения                   | Не путать с Celery broker DB                                                |
| `celery_redis_uri`                      | Redis base URI для Celery             | Обычно без номера DB на конце                                               |
| `celery_broker_db`                      | Redis DB для очередей Celery          | Метрики очередей читают именно эту DB                                       |
| `celery_backend_db`                     | Redis DB для результатов Celery       | Не используйте ее для диагностики queue length                              |
| `celery_default_queue`                  | Основная очередь worker               | По умолчанию `celery`                                                       |
| `celery_visibility_timeout`             | Таймаут повторной доставки задач      | Должен покрывать долгие embedder/crawler задачи                             |
| `celery_worker_concurrency`             | Concurrency обычного Celery worker    | Учитывайте CPU/RAM requests deployment                                      |
| `celery_worker_max_tasks_per_child`     | Перезапуск child-процессов Celery     | Помогает ограничить накопление памяти                                       |
| `celery_worker_max_memory_per_child_kb` | Memory cap Celery child               | Согласуйте с pod memory limit                                               |
| `embedding_model_id`                    | Hugging Face model id для embeddings  | При `DOWNLOAD_MODELS=true` модель скачивается в image                       |
| `embedding_model_dir`                   | Путь к модели embeddings              | Без runtime volume модель должна быть внутри image                          |
| `reranker_model_id`                     | Hugging Face model id reranker        | Тоже скачивается при `entry.py --model`                                     |
| `embedding_worker_instances`            | Количество embedder worker внутри pod | В Kubernetes обычно задавайте `EMBEDDER_INSTANCES=1` и масштабируйте pods   |
| `embedding_worker_cpu_reserve`          | CPU reserve для auto режима embedder  | Не используйте auto при жестких pod limits без проверки                     |
| `log_format`                            | Формат логов                          | Для Kubernetes/Grafana Loki обычно `json`                                   |
| `chat_provider`                         | Провайдер LLM                         | Для GigaChat ставьте `gigachat`                                             |
| `chat_model`                            | Модель LLM                            | Должна соответствовать договорной production-модели                         |
| `gigachat_verify_ssl_certs`             | SSL verification GigaChat             | В production оставляйте `true`                                              |
| `auth_basic_enabled`                    | Локальный логин/пароль                | В production часто должен быть `false`                                      |
| `auth_ldap_enabled`                     | LDAP login                            | Включите, если вход идет через AD/LDAP                                      |

> Комментарий: Redis в проекте разделен на DB. `redis_uri` — это
> app Redis, а Celery queues находятся в `celery_broker_db`. Если смотреть
> `LLEN embeddings` не в той DB, можно ошибочно решить, что очередь пустая.

### 14.5. Подготовка manifests под среду

Перед применением замените примерные значения:

1. `deploy/k8s/base/kustomization.yaml`: image registry и tag.
2. `deploy/k8s/base/configmap.yaml`: домены, Redis адрес, non-secret settings.
3. `deploy/k8s/base/ingress.yaml`: `host` и `ingressClassName`.
4. `deploy/k8s/base/networkpolicy.yaml`: namespace ingress controller и monitoring.
5. `deploy/k8s/base/secret.example.yaml`: используйте только как шаблон, не
   применяйте с `change-me`.

Проверка сгенерированного YAML без применения:

```bash
kubectl kustomize deploy/k8s/base >/tmp/vchat-k8s-base.yaml
kubectl kustomize deploy/k8s/keda >/tmp/vchat-k8s-keda.yaml
kubectl kustomize deploy/k8s/monitoring >/tmp/vchat-k8s-monitoring.yaml
```

### 14.6. Порядок первичного развертывания

1. Соберите и опубликуйте image.
2. Создайте namespace, если он еще не создан.
3. Создайте `Secret/vchat-secret`.
4. Убедитесь, что PostgreSQL доступен и в базе включен `pgvector`.
5. Убедитесь, что Redis доступен из namespace `vchat`.
6. Примените base manifests.
7. Дождитесь успешного завершения `Job/vchat-migrate`.
8. Проверьте rollout web, Celery и embedder.
9. Подключите monitoring overlay, если установлен Prometheus Operator.
10. Подключите KEDA overlay, если установлен KEDA.

Команды:

```bash
kubectl apply -k deploy/k8s/base
kubectl -n vchat wait --for=condition=complete job/vchat-migrate --timeout=300s
kubectl -n vchat rollout status deployment/vchat-web
kubectl -n vchat rollout status deployment/vchat-celery
kubectl -n vchat rollout status deployment/vchat-embedder
```

Если миграция упала:

```bash
kubectl -n vchat logs job/vchat-migrate
```

Не запускайте несколько migration jobs одновременно для одного окружения.

### 14.7. Probes и health checks

В web deployment используются:

| Probe            | Endpoint        | Смысл                                                   |
| ---------------- | --------------- | ------------------------------------------------------- |
| `startupProbe`   | `/health/ready` | Дает приложению время стартовать и прогреть зависимости |
| `readinessProbe` | `/health/ready` | Проверяет готовность принимать трафик                   |
| `livenessProbe`  | `/health/live`  | Проверяет, что процесс web жив                          |

`/health/ready` проверяет PostgreSQL и Redis. Если DB или Redis недоступны,
pod станет not ready, и Service перестанет направлять на него пользовательский
трафик.

Проверка из кластера:

```bash
kubectl -n vchat run curl-check --rm -it --restart=Never \
  --image=curlimages/curl -- \
  curl -fsS http://vchat-web:9080/health/ready
```

### 14.8. Метрики и Grafana

Приложение отдает Prometheus metrics на `/metrics`. В Kubernetes этот endpoint
должен собираться только внутри кластера:

```text
Service/vchat-metrics -> pods vchat-web -> /metrics
```

Public Ingress не должен отдавать `/metrics` наружу. В базовом ingress exact
`/metrics` и prefix `/metrics/` направлены в `vchat-blackhole`.

Если установлен Prometheus Operator:

```bash
kubectl apply -k deploy/k8s/monitoring
```

Если Prometheus Operator не используется, настройте scrape вручную:

```yaml
scrape_configs:
  - job_name: vchat
    metrics_path: /metrics
    static_configs:
      - targets:
          - vchat-metrics.vchat.svc.cluster.local:9080
```

Alert rules:

```text
deploy/prometheus_alerts.yml
```

Ключевые метрики:

| Метрика                                      | Что показывает                                    | Когда реагировать                                         |
| -------------------------------------------- | ------------------------------------------------- | --------------------------------------------------------- |
| `vchat_chat_requests_total`                  | Количество chat-запросов по provider/model/status | Резкий рост ошибок или guardrail блокировок               |
| `vchat_chat_response_duration_seconds`       | Время ответа chat pipeline                        | Рост p95/p99 ухудшает пользовательский опыт               |
| `vchat_chat_tokens_total`                    | Расход LLM tokens                                 | Контроль стоимости и аномалий                             |
| `vchat_chat_context_chunks`                  | Сколько chunks попало в контекст                  | Нули или резкое падение могут означать проблему retrieval |
| `vchat_chat_guardrail_events_total`          | Guardrail events                                  | Рост может означать некорректные запросы или policy issue |
| `vchat_crawler_pages_total`                  | Результаты обработки страниц crawler              | Рост `result="error"` требует проверки источников         |
| `vchat_crawler_run_duration_seconds`         | Длительность crawl runs                           | Рост может означать проблемы сети или источника           |
| `vchat_crawler_rate_limited_total`           | Rate limiting источников                          | Нужно снижать частоту/параллелизм crawl                   |
| `vchat_crawler_last_crawl_timestamp_seconds` | Время последнего crawl по source                  | Старые источники требуют диагностики scheduler/worker     |
| `vchat_celery_queue_size`                    | Длина основной очереди `celery`                   | Очередь растет быстрее обработки                          |
| `vchat_embedder_queue_size`                  | Длина очереди `embeddings`                        | Не хватает embedder capacity или упали workers            |
| `vchat_crawler_queue_size`                   | Длина очереди `crawler`                           | Проверить crawler workers и источники                     |
| `vchat_active_chats`                         | Активные websocket chat sessions                  | Для оценки текущей нагрузки                               |

> Комментарий: метрики очередей читаются из Redis Celery broker DB.
> Если `celery_broker_db` поменяли, Prometheus автоматически начнет читать новую
> DB, потому что collector использует значения из `local.yaml`.

### 14.9. Логи

Для Kubernetes ставьте:

```yaml
log_format: "json"
```

Быстрые команды:

```bash
kubectl -n vchat logs deployment/vchat-web --tail=200
kubectl -n vchat logs deployment/vchat-celery --tail=200
kubectl -n vchat logs deployment/vchat-embedder --tail=200
kubectl -n vchat logs deployment/vchat-celery-beat --tail=200
```

На что смотреть:

| Логи                | Типичные проблемы                                           |
| ------------------- | ----------------------------------------------------------- |
| `vchat-web`         | DB/Redis errors, LDAP errors, GigaChat OAuth/SSL/timeouts   |
| `vchat-celery`      | crawler failures, task retries, Redis broker issues         |
| `vchat-embedder`    | model loading errors, memory pressure, long embedding tasks |
| `vchat-celery-beat` | расписание задач, ошибки подключения к broker               |

### 14.10. Масштабирование

Масштабируются разные компоненты по разным сигналам.

Для `HorizontalPodAutoscaler` нужен Kubernetes metrics-server или совместимый
источник resource metrics. Для `deploy/k8s/keda/` нужен установленный KEDA
operator и его CRD.

| Компонент           | Как масштабировать                | Сигнал                                               |
| ------------------- | --------------------------------- | ---------------------------------------------------- |
| `vchat-web`         | HPA по CPU или вручную `replicas` | CPU, latency, active chats                           |
| `vchat-celery`      | KEDA по queue length или вручную  | `vchat_celery_queue_size`, Redis `LLEN celery`       |
| `vchat-embedder`    | KEDA по queue length или вручную  | `vchat_embedder_queue_size`, Redis `LLEN embeddings` |
| `vchat-celery-beat` | Не масштабировать                 | Всегда 1 replica                                     |
| `vchat-migrate`     | Не масштабировать                 | Запускается один раз на deploy                       |

Ручное масштабирование:

```bash
kubectl -n vchat scale deployment/vchat-web --replicas=4
kubectl -n vchat scale deployment/vchat-celery --replicas=4
kubectl -n vchat scale deployment/vchat-embedder --replicas=2
```

KEDA overlay:

```bash
kubectl apply -k deploy/k8s/keda
```

По умолчанию KEDA смотрит:

| ScaledObject     | Redis list   | DB   |
| ---------------- | ------------ | ---- |
| `vchat-celery`   | `celery`     | `31` |
| `vchat-embedder` | `embeddings` | `31` |

Если меняете `celery_broker_db` или queue names, синхронно обновите:

- `deploy/k8s/base/configmap.yaml`;
- `deploy/k8s/keda/scaledobject-celery.yaml`;
- `deploy/k8s/keda/scaledobject-embedder.yaml`;
- Grafana dashboards/alerts, если там queue names захардкожены.

Для embedder в Kubernetes рекомендуемый режим:

```yaml
env:
  - name: EMBEDDER_INSTANCES
    value: "1"
```

Масштабируйте embedder количеством pods, а не внутренним auto fan-out внутри
одного pod. Так scheduler Kubernetes лучше контролирует CPU/RAM.

### 14.11. Ресурсы и memory pressure

Базовые requests/limits:

| Deployment          | Requests        | Limits          |
| ------------------- | --------------- | --------------- |
| `vchat-web`         | `500m`, `1Gi`   | `2 CPU`, `2Gi`  |
| `vchat-celery`      | `500m`, `1Gi`   | `2 CPU`, `2Gi`  |
| `vchat-celery-beat` | `100m`, `256Mi` | `500m`, `512Mi` |
| `vchat-embedder`    | `2 CPU`, `4Gi`  | `4 CPU`, `8Gi`  |

Если pods получают `OOMKilled`:

1. Посмотрите `kubectl describe pod`.
2. Проверьте, какой контейнер падает.
3. Для embedder сначала уменьшите batch/concurrency или увеличьте memory limit.
4. Для Celery проверьте `celery_worker_max_memory_per_child_kb`.
5. Для web проверьте всплески chat latency и active sessions.

Команды:

```bash
kubectl -n vchat get pods
kubectl -n vchat describe pod <POD>
kubectl -n vchat top pods
```

### 14.12. Безопасность контейнеров

Базовые manifests включают:

- `runAsNonRoot: true`;
- фиксированные `runAsUser: 10001` и `runAsGroup: 10001`;
- `allowPrivilegeEscalation: false`;
- `readOnlyRootFilesystem: true`;
- dropped Linux capabilities;
- `seccompProfile: RuntimeDefault`;
- `ServiceAccount` без automount token;
- writable только `/tmp`;
- `NetworkPolicy` для ingress к web pods;
- internal-only сервис для metrics.

Что должен обеспечить внешний контур:

- TLS на Ingress;
- secret management;
- image scanning, например Trivy;
- immutable registry tags или digest pinning;
- ограничение доступа к registry;
- сбор audit/security logs средствами платформы.

### 14.13. Операционный чеклист Kubernetes

После deploy проверьте:

```bash
kubectl -n vchat get pods
kubectl -n vchat get deploy
kubectl -n vchat get hpa
kubectl -n vchat get ingress
kubectl -n vchat get svc
kubectl -n vchat logs job/vchat-migrate
```

Проверки результата:

| Проверка                         | Ожидаемый результат                             |
| -------------------------------- | ----------------------------------------------- |
| `job/vchat-migrate`              | `Complete`                                      |
| `deployment/vchat-web`           | Все replicas ready                              |
| `deployment/vchat-celery`        | Все replicas ready, нет crash loop              |
| `deployment/vchat-celery-beat`   | 1 replica ready                                 |
| `deployment/vchat-embedder`      | Replicas ready, модель загружена                |
| `/health/ready`                  | HTTP 200 внутри кластера                        |
| `/metrics` через `vchat-metrics` | Prometheus text format                          |
| `/metrics` через public Ingress  | Не должен отдавать метрики наружу               |
| Grafana dashboard                | Видны queue size, chat latency, crawler metrics |
| Redis queue length               | Не растет бесконечно                            |

### 14.14. Типовые Kubernetes-проблемы

| Симптом                                              | Что проверять                                                                  |
| ---------------------------------------------------- | ------------------------------------------------------------------------------ |
| Web pod not ready                                    | `DATABASE_URI`, Redis, `/health/ready`, logs web                               |
| Миграция упала                                       | `kubectl logs job/vchat-migrate`, доступ к DB, наличие `pgvector`              |
| `/metrics` доступен снаружи                          | Ingress rules для `/metrics`, порядок paths, controller-specific behavior      |
| Prometheus не собирает метрики                       | `ServiceMonitor`, labels `app.kubernetes.io/component=metrics`, network policy |
| Очередь `embeddings` растет                          | Logs embedder, KEDA, model files, memory limit                                 |
| Очередь `celery` растет                              | Logs celery, Redis broker DB, количество worker pods                           |
| Дублируются плановые задачи                          | Убедиться, что `vchat-celery-beat` имеет `replicas: 1`                         |
| Pods `CrashLoopBackOff` после включения read-only FS | Проверить, что процесс пишет только в `/tmp`                                   |
| KEDA не масштабирует                                 | Проверить установлен ли KEDA, Redis address, DB index, list name               |

## 15. Обновление проекта

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

> Комментарий: не запускайте миграции “на всякий случай” из
> нескольких мест одновременно. Миграции должны выполняться один раз на
> окружение в рамках контролируемого деплоя.

## 16. Проверочный чеклист после запуска

| Шаг                                 | Ожидаемый результат                                |
| ----------------------------------- | -------------------------------------------------- |
| Открыть страницу входа              | Видна форма входа                                  |
| Войти через LDAP                    | Пользователь попадает в админку                    |
| Проверить пользователя в списке     | У пользователя стоит `is_ldap`                     |
| Открыть `/metrics`                  | Возвращается Prometheus text format                |
| Добавить тестовый источник или файл | Источник сохраняется                               |
| Запустить или дождаться индексации  | Создаются chunks и embeddings                      |
| Задать вопрос в виджете             | Ответ содержит релевантный текст и источники       |
| Проверить логи                      | Нет повторяющихся ошибок LDAP, Redis, DB, GigaChat |

## 17. Типовые проблемы

| Симптом                                                 | Что делать                                                                   |
| ------------------------------------------------------- | ---------------------------------------------------------------------------- |
| LDAP-вход всегда отклоняется                            | Проверить `ldap_search_base`, `ldap_search_filter`, service account и пароль |
| LDAP-вход работает без группы, но не работает с группой | Проверить полный DN группы и наличие `memberOf`                              |
| Пользователь входит, но документы не индексируются      | Проверить Celery worker и очередь `celery`                                   |
| Chunks есть, но embeddings не появляются                | Проверить embedder worker и очередь `embeddings`                             |
| GigaChat не отвечает                                    | Проверить `gigachat_api_key`, OAuth URL, SSL и сетевой доступ                |
| Сессия живет слишком долго                              | Уменьшить `session_max_age_seconds` или включить `auth_session_time`         |
| Все пользователи должны выйти из системы                | Ротировать `cookie_key` и перезапустить web                                  |

## 18. Что не надо делать без согласования

- Не коммитьте `local.yaml` с секретами.
- Не меняйте `vchat/config.yaml` для разовых production-секретов.
- Не отключайте SSL-проверку GigaChat без письменного решения.
- Не включайте basic-auth в production, если политика Заказчика требует только LDAP.
- Не запускайте несколько несовместимых Celery broker DB для одного окружения.
- Не чистите Redis DB целиком без понимания, какие очереди и временные ключи там лежат.
- Не ротируйте `cookie_key` в рабочее время без предупреждения пользователей.
- Не публикуйте `/metrics` через внешний Ingress без отдельного письменного решения.
- Не масштабируйте `vchat-celery-beat` выше одной replica.

## 19. Минимальный production-пример `local.yaml`

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
auth_session_time: 43200
max_upload_size: 5242880
raw_content_max_bytes: 10485760
secret_key: !env "$SECRET_KEY"
cookie_key: !env "$COOKIE_KEY"
vchat_secret: !env "$VCHAT_SECRET"

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

> Комментарий: этот пример нельзя копировать в production без
> замены доменов, паролей и DN. Он нужен как форма, которую удобно заполнить
> вместе с инфраструктурной командой Заказчика.
