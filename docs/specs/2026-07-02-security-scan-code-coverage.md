# Полное закрытие security scan и ASVS-чеклиста

## Goal

Доказать по текущему коду, что каждый finding из `docs/reports/security-scan-20260613.md` закрыт реализацией и тестами, а не только отчётами или случайными артефактами. После исправлений обновить `docs/asvs_l1_l2_checklist.md` так, чтобы пункты, завязанные на security scan, ссылались на реальные code/test evidence.

## Context

- `frontend_chat/src/safe-markdown.js` уже заменяет прямой `marked.parse` allowlist-renderer'ом и экранирует HTML/image tokens.
- `vchat/middlewares/__init__.py` инвалидирует сессию, если активный пользователь больше не найден с `User.is_active.is_(True)`.
- `vchat/views/auth/views.py` отклоняет inactive LDAP users и не даёт LDAP-вход локальным пользователям.
- `vchat/views/api/views.py` вручную обрабатывает redirect и ограничивает размер тела, но не проверяет DNS/IP initial и redirect URL перед fetch.
- `jobs/crawler/source_blocking.py` блокирует private/special source origins, но `jobs/crawler/tasks.py` всё ещё использует `allow_redirects=True` для robots/probe sitemap discovery.
- `vchat/views/projects/views.py` защищает file editor POST через `validate_signed_user_csrf`, но CSV export пишет attacker-influenced поля напрямую через `csv.DictWriter`.
- `vchat/routes.py` больше не монтирует `/data/`, а staged `deploy/Dockerfile.dockerignore` исключает `data/*`; при этом `/static/` всё ещё смонтирован с `follow_symlinks=True`, потому что локальный `static/` является набором symlink'ов на build-директории.

## Current Behavior

Audit security scan findings:

| ID | Статус по коду | Evidence / gap |
| --- | --- | --- |
| `VCHAT-CHAT-MARKED-XSS-001` | Закрыто | safe Markdown renderer, template больше не вызывает `marked.parse`, есть template assertions. |
| `AUTH-SESSION-INACTIVE-001` | Закрыто | auth middleware выбирает только active users и инвалидирует stale session. |
| `VCHAT-API-UPDATE-SSRF-REDIRECT-TOCTOU` | Частично | Cross-domain redirects запрещены, но нет DNS/IP guard для same-domain private targets. |
| `VCHAT-CONFIG-DEFAULT-SECRETS-001` | Закрыто | production config падает на default/placeholder secrets; есть tests. |
| `VCHAT-CRAWLER-UNBOUNDED-DOWNLOAD-DOS-003` | Закрыто | sitemap/robots/probe/API reads ограничены Content-Length и streaming cap; Scrapy `DOWNLOAD_MAXSIZE` выставлен. |
| `VCHAT-CRAWLER-SOURCE-SSRF-001` | Частично | Source origins проходят private IP blocking, но robots/probe requests следуют redirect'ам автоматически. |
| `VCHAT-PROJ-002` | Не закрыто | `project_documents_csv` пишет title/uri/source без formula neutralization. |
| `VCHAT-PROJ-001` | Закрыто | file create/save/delete POST проверяют signed CSRF; есть rejection tests. |
| `AUTH-LDAP-LOCAL-STATE-002` | Закрыто | LDAP login reject'ит inactive и existing local users; LDAP filter escaped. |
| `VCHAT-PUB-RAG-GLOBAL-KB-001` | Закрыто | widget WebSocket вызывает `get_context(..., allowed_source_ids=[])`. |
| `VCHAT-CRAWLER-SITEMAP-OFFHOST-SSRF-002` | Закрыто | `_is_valid_sitemap_address` требует host source URI для sitemap и urlset page URLs. |
| `VCHAT-API-UPDATE-UNBOUNDED-READ-DOS` | Закрыто | `_fetch_url_content` читает чанками и падает при превышении `raw_content_max_bytes`. |
| `VCHAT-DATA-STATIC-EXPOSURE-001` | Частично | `/data/` route отсутствует и Docker ignore исключает `data/*`, но публичный static route всё ещё разрешает symlink traversal для build symlink'ов. |

## Target Shape

- Для каждого security finding есть одно из двух состояний: `Covered` с конкретным code/test evidence или `Accepted external` с явно утверждённой внешней причиной. Для текущего запроса внешние артефакты не считаются достаточным покрытием.
- CSV export нейтрализует formula-leading cells в attacker-controlled text fields.
- API update не делает HTTP-запросы к private, loopback, link-local, multicast, unspecified и другим non-global адресам ни на initial URL, ни после redirect.
- Crawler discovery не следует redirect'ам автоматически там, где redirect target не прошёл same-host и IP guard.
- Публичная static-раздача не зависит от общего `follow_symlinks=True`; ассеты отдаются из явных build-директорий или другого ограниченного маршрута.
- `docs/asvs_l1_l2_checklist.md` обновлён по всем затронутым ASVS L1/L2 пунктам с evidence на код/тесты.

## Guard Rails

- Не использовать remote server access.
- Не добавлять graceful fallback/legacy shim: security checks должны fail-fast отклонять опасные inputs.
- Не трогать чужие текущие изменения вне нужных строк.
- Не считать `docs/reports/*`, `security/*`, `htmlcov/*` самостоятельным evidence реализации.
- Для статических ассетов не ломать локальную и Docker-раздачу frontend/frontend_chat build outputs.

## Iterations

1. CSV formula injection:
   - Добавить маленький helper для neutralization строковых CSV cells.
   - Применить к `title`, `uri`, `source`, `status`, `status_error`; числовые поля оставить числовыми строками.
   - Тесты: `=`, `+`, `-`, `@`, tab, carriage return/newline-leading values.

2. API update SSRF hardening:
   - Вынести проверку fetchable HTTP URL/host address в общий helper без сетевых fallbacks.
   - Проверять initial URL и каждый redirect target перед `client.get`.
   - Тесты: same-domain redirect на host, который резолвится в private IP, должен падать до второго fetch.

3. Crawler redirect hardening:
   - Заменить `allow_redirects=True` в robots/probe discovery на ручной redirect handling или отказ от redirect.
   - Переиспользовать same-host + non-private address policy.
   - Тесты на robots/probe redirect к private/cross-domain target.

4. Static/data exposure:
   - Убедиться тестом, что `/data/` route отсутствует.
   - Убрать общий `follow_symlinks=True` для публичной раздачи либо заменить symlink-based `/static/` на явные маршруты к build directories.
   - Добавить route-level тест, фиксирующий отсутствие symlink-following для произвольных repo paths.

5. Checklist/report sync:
   - Обновить `docs/asvs_l1_l2_checklist.md` для закрытых security-scan пунктов.
   - Добавить краткий audit note в checklist comments: code evidence only.

## Verification

- `venv/bin/pytest tests/test_api_views.py tests/test_crawler_tasks.py tests/test_source_blocking.py tests/test_projects_actions_extended.py tests/test_projects_views_and_chats.py tests/test_utils_and_settings.py`
- Точечный `rg` pass:
  - `rg -n "marked\\.parse|innerHTML|allow_redirects=True|follow_symlinks=True|DictWriter" frontend_chat vchat jobs tests`
  - Каждый найденный sink должен иметь безопасный wrapper, явный allowlist или тестовое объяснение.

## Open Questions

- Подтверждаешь, что для static-директории можно заменить текущую symlink-схему на явные build asset routes, если это минимально затронет локальную разработку?
- Для CSV neutralization используем префикс `'` как наиболее совместимый вариант?
