# Task: Обновление зависимостей Python и Node.js

## Goal
Install current compatible stable dependencies and reject compromised eMudhra TLS chains in project and tool trust stores.

## Context
Project dependency manifests, lockfiles and existing build/deployment scripts.

## Guard Rails
Preserve unrelated changes, service configuration, secrets, database data and existing user behavior. Use an isolated branch and commit only this task. Keep existing runtime/API constraints unless a verified upgrade requires a change. No prereleases.

## Iterations
1. Record versions and update dependency manifests and locks from official registries.
2. Install and run project checks/builds; resolve compatibility failures.
3. Commit and publish the tested revision, deploy via existing scripts or equivalent explicit manual steps when CI is unavailable.
4. Verify running revisions, service readiness, public pages, both compromised TLS chains and a legitimate HTTPS control.

## Verification
Both eMudhra diagnostic hosts must fail certificate verification with default TLS verification enabled. example.com must succeed. Dependency consistency checks, relevant tests and frontend builds must pass; disclose existing or environmental failures. Preserve a rollback point before server changes.

## Open Questions
None blocking; current supported runtime constraints define compatibility.

## Проверка
- Обе сборки интерфейса прошли с обновлёнными пакетами.
- Полный набор тестов на изолированных PostgreSQL/Redis и подготовленном Linux-окружении: 785 passed, 2 skipped.
- Проверка согласованности пакетов в конечном CUDA-окружении прошла.
- Два последовательных разрешения зависимостей заменены одним полным lock-файлом Linux/CUDA: повторная установка PyTorch больше не меняет Pillow и другие общие пакеты.
- Нативные библиотеки GPU требуют согласованного драйвера NVIDIA; выпуск окружения ожидает перезагрузки сервера.
