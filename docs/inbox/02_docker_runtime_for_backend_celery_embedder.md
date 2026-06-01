# Docker Runtime For Backend, Celery, And Embedder

Нужно собрать Docker-файлы и Docker-окружение, внутри которого можно запускать:

- `backend`
- `celery`
- `embedder`

Обязательно учесть системные зависимости для Python-модулей, которые не ставятся на голой системе.

Отдельно зафиксировать как обязательные системные зависимости для `bonsai`:

- `libldap2-dev`
- `libsasl2-dev`

Причина: без LDAP headers, в частности `ldap.h`, `bonsai` не собирается и не устанавливается.
