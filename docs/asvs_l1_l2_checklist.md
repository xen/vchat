# OWASP ASVS L1+L2 Checklist for vchat

Документ закрывает пункт `62` из `docs/contract_gap_reconciliation.md`: OWASP ASVS LVL1+LVL2 для web-приложения.

## Scope

- Standard: OWASP Application Security Verification Standard 5.0.0.
- Source: https://github.com/OWASP/ASVS/tree/v5.0.0/5.0/en
- Exact requirement text is intentionally not copied here; use `v5.0.0-<id>` against the official ASVS source.
- Assessment target: `/Users/xen/Dev/sber/vchat` web application, background jobs, deployment manifests, security reports, and operational documentation in this repository.
- Runtime/server-only controls must be marked `Needs runtime evidence` until confirmed by deployment evidence or customer infrastructure documents.

## Status Legend

- `Covered` - enough code, test, configuration, report, or accepted document evidence exists in this repository.
- `Partial` - a control exists, but scope, runtime evidence, or documentation is incomplete.
- `Missing` - no sufficient implementation or evidence found yet.
- `N/A` - not applicable to this application; reason must be recorded.
- `Accepted external` - control is deliberately implemented by customer infrastructure, AD, registry, SIEM, network, or process; external owner/evidence must be recorded.
- `TBD` - not reviewed yet.

## Evidence Index

- `security/security-report.html` - local security-check aggregate report.
- `security/security-summary.json` - Semgrep, Gitleaks, OSV, Syft/SBOM, Trivy, Ruff, Bandit summary.
- `security/security-proof.txt` - security-check run metadata and artifact hashes.
- `docs/reports/security-scan-20260613.md` - repository-wide Codex security review.
- `bin/security-check.sh` and `bin/security-report.py` - repeatable security scan tooling.
- `deploy/README.md` and `deploy/k8s/base/*` - deployment and container hardening evidence.
- `docs/manual.md` - operational manual evidence where applicable.
- `tests/` - regression evidence; cite focused tests per control.

## Assessment Summary

- Total ASVS 5.0.0 L1+L2 requirements in this checklist: 253.
- Current review status: initial scaffold plus first pass over clearly
  applicable/non-applicable controls.
- Current counts:
  - `Covered`: 38
  - `Partial`: 0
  - `Missing`: 0
  - `N/A`: 43
  - `Accepted external`: 0
  - `TBD`: 172

## V1 Encoding and Sanitization

### V1.1 Encoding and Sanitization Architecture

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-1.1.1` | 2 | TBD |  |
| `v5.0.0-1.1.2` | 2 | Covered | Jinja autoescaping, `tojson`, and DOM text APIs are used for templates/browser code; assistant Markdown now goes through `frontend_chat/src/safe-markdown.js::renderSafeAssistantMarkdown`, which escapes raw HTML/image tokens before the template writes renderer output to `innerHTML`; covered by `tests/test_projects_views_and_chats.py`. |
### V1.2 Injection Prevention

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-1.2.1` | 1 | Covered | Server templates use Jinja autoescaping and browser paths use `escapeHtml`/`textContent`; assistant Markdown XSS finding is covered by `frontend_chat/src/safe-markdown.js` and template tests asserting `marked.parse` is not used. |
| `v5.0.0-1.2.2` | 1 | Covered | URL metadata validation rejects non-http(s), protocol-relative, and relative values; see `vchat/views/chat/meta.py::validate_source_page_url` and `tests/test_utils_and_settings.py`. |
| `v5.0.0-1.2.3` | 1 | Covered | JSON/JS values use `tojson`, DOM text APIs, `escapeHtml`, SafeHTML validators, or static application templates/SVG; chat error text now uses `textContent`, and chat template tests assert safe Markdown renderer usage and no direct `marked.parse`. |
| `v5.0.0-1.2.4` | 1 | Covered | SQLAlchemy ORM/bound parameters are the normal query path; security tooling reports no SQL injection finding in `security/security-summary.json` and `docs/reports/security-scan-20260613.md`. |
| `v5.0.0-1.2.5` | 1 | Covered | Runtime subprocess use passes argument lists; no `shell=True` runtime path found in the reviewed code, and Bandit/Semgrep reports are clean in `security/security-summary.json`. |
| `v5.0.0-1.2.6` | 2 | Covered | LDAP search input is escaped and covered by `tests/test_middlewares_auth_user.py::test_authenticate_ldap_escapes_search_filter_email`; LDAP enablement is runtime-configured. |
| `v5.0.0-1.2.7` | 2 | N/A | No XPath query surface found in application/runtime code. |
| `v5.0.0-1.2.8` | 2 | N/A | No LaTeX processing surface found in application/runtime code. |
| `v5.0.0-1.2.9` | 2 | Covered | Trigger regex and crawler URL regex rules enforce length limits and reject lookaround, backreferences, and nested repeating groups before use; covered by `tests/test_triggers.py` and `tests/test_crawler_overhaul.py::test_url_allowed_by_rules_rejects_expensive_regex_filters`. |
### V1.3 Sanitization

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-1.3.1` | 1 | Covered | Widget rich-text fields have sanitization tests in `tests/test_projects_forms_views_more.py`; assistant Markdown uses the allowlist renderer in `frontend_chat/src/safe-markdown.js` and no longer renders raw Marked HTML. |
| `v5.0.0-1.3.2` | 1 | Covered | No application/runtime use of Python `eval()` or equivalent dynamic code execution found; security scan did not report this class. |
| `v5.0.0-1.3.3` | 2 | TBD |  |
| `v5.0.0-1.3.4` | 2 | N/A | No user-supplied SVG upload/rendering surface identified; built-in widget SVG is static application code. |
| `v5.0.0-1.3.5` | 2 | Covered | Assistant Markdown is rendered by `renderSafeAssistantMarkdown`, which escapes `html`, `image`, and unsupported tokens; `tests/test_projects_views_and_chats.py` asserts chat templates use the safe renderer instead of `marked.parse`. |
| `v5.0.0-1.3.6` | 2 | Covered | API update fetches validate each initial/redirect target with DNS/IP guards before `GET`; crawler source blocking rejects private/special addresses and sitemap/robots discovery no longer auto-follows redirects; covered by `tests/test_api_views.py`, `tests/test_source_blocking.py`, and `tests/test_crawler_tasks.py`. |
| `v5.0.0-1.3.7` | 2 | Covered | No untrusted template construction path found; templates are static repo files and user content is passed as data. |
| `v5.0.0-1.3.8` | 2 | N/A | No JNDI/Java runtime surface. |
| `v5.0.0-1.3.9` | 2 | N/A | No memcache use; runtime cache/queues use Redis. |
| `v5.0.0-1.3.10` | 2 | TBD |  |
| `v5.0.0-1.3.11` | 2 | N/A | No SMTP/IMAP sending surface found in application/runtime code. |
### V1.4 Memory, String, and Unmanaged Code

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-1.4.1` | 2 | N/A | Python web application; no unmanaged memory/string API surface in project runtime code. |
| `v5.0.0-1.4.2` | 2 | N/A | Python arbitrary-precision integers and ORM/database constraints cover ordinary runtime; no unmanaged numeric arithmetic surface identified. |
| `v5.0.0-1.4.3` | 2 | N/A | Python garbage-collected runtime; no manual memory management surface identified. |
### V1.5 Safe Deserialization

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-1.5.1` | 1 | Covered | Runtime sitemap XML parsing uses `defusedxml.ElementTree` in `jobs/crawler/tasks.py`; Office/PDF extraction is size-capped before parsing and covered by document pipeline tests. |
| `v5.0.0-1.5.2` | 2 | TBD |  |
## V2 Validation and Business Logic

### V2.1 Validation and Business Logic Documentation

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-2.1.1` | 1 | TBD |  |
| `v5.0.0-2.1.2` | 2 | TBD |  |
| `v5.0.0-2.1.3` | 2 | TBD |  |
### V2.2 Input Validation

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-2.2.1` | 1 | TBD |  |
| `v5.0.0-2.2.2` | 1 | TBD |  |
| `v5.0.0-2.2.3` | 2 | TBD |  |
### V2.3 Business Logic Security

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-2.3.1` | 1 | TBD |  |
| `v5.0.0-2.3.2` | 2 | TBD |  |
| `v5.0.0-2.3.3` | 2 | TBD |  |
| `v5.0.0-2.3.4` | 2 | TBD |  |
### V2.4 Anti-automation

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-2.4.1` | 2 | TBD |  |
## V3 Web Frontend Security

### V3.2 Unintended Content Interpretation

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-3.2.1` | 1 | Covered | `security_headers_middleware` sets `X-Content-Type-Options: nosniff` for app responses; covered by `tests/test_middlewares_auth_user.py::test_security_headers_middleware_sets_browser_headers`. |
| `v5.0.0-3.2.2` | 1 | Covered | User-visible chat rendering uses escaping/text APIs or the safe assistant Markdown renderer; the prior `VCHAT-CHAT-MARKED-XSS-001` path no longer calls `marked.parse` directly. |
### V3.3 Cookie Setup

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-3.3.1` | 1 | Covered | Session uses `EncryptedCookieStorage` with configured name/domain/secure/max_age; production config sets `cookie_secure: true` in `vchat/config.yaml` and `deploy/k8s/base/configmap.yaml`. |
| `v5.0.0-3.3.2` | 2 | Covered | Cookie domain/path/max_age are centrally configured in `vchat/middlewares/__init__.py`; production manual documents `cookie_domain` and `cookie_secure`. |
| `v5.0.0-3.3.3` | 2 | Covered | Session cookie storage is configured with `secure=config["cookie_secure"]`; production defaults set `cookie_secure: true`, and tests assert the configured storage parameters. |
| `v5.0.0-3.3.4` | 2 | Covered | `EncryptedCookieStorage` is configured with `httponly=True` and `samesite="Lax"`; covered by `tests/test_middlewares_auth_user.py::test_get_middlewares_uses_configured_session_max_age`. |
### V3.4 Browser Security Mechanism Headers

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-3.4.1` | 1 | Covered | `security_headers_middleware` sets CSP, HSTS for HTTPS requests, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, and `X-Frame-Options`; covered by middleware tests. |
| `v5.0.0-3.4.2` | 1 | Covered | Core browser security headers are applied centrally in `vchat/middlewares/__init__.py::security_headers_middleware`. |
| `v5.0.0-3.4.3` | 2 | Covered | Application-level CSP is set centrally, including `default-src 'self'`, `object-src 'none'`, `base-uri 'self'`, and `frame-ancestors 'self'`. |
| `v5.0.0-3.4.4` | 2 | Covered | HTTPS requests receive `Strict-Transport-Security: max-age=31536000; includeSubDomains`; test covers the HTTPS branch. |
| `v5.0.0-3.4.5` | 2 | Covered | `Referrer-Policy: strict-origin-when-cross-origin` is set centrally by security headers middleware. |
| `v5.0.0-3.4.6` | 2 | Covered | `Permissions-Policy` disables camera, microphone, geolocation, and payment by default in application responses. |
### V3.5 Browser Origin Separation

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-3.5.1` | 1 | Covered | Production middleware now calls CORS with `allow_all=False` and configured `allowed_origins`/`public_url`; covered by `tests/test_middlewares_auth_user.py::test_get_middlewares_uses_configured_cors_origins`. |
| `v5.0.0-3.5.2` | 1 | Covered | Widget postMessage code checks `event.origin` against `widgetOrigin()` and sends messages to explicit target origins in `vchat/templates/js/widget.js`. |
| `v5.0.0-3.5.3` | 1 | Covered | App responses set `X-Frame-Options: SAMEORIGIN` and CSP `frame-ancestors 'self'`; public widget embedding remains through explicit widget loader behavior. |
| `v5.0.0-3.5.4` | 2 | Covered | CSRF is signed for form/action endpoints and WebSocket handshakes reject disallowed `Origin` values via `_websocket_origin_allowed`; covered by chat/middleware tests. |
| `v5.0.0-3.5.5` | 2 | TBD |  |
### V3.7 Other Browser Security Considerations

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-3.7.1` | 2 | TBD |  |
| `v5.0.0-3.7.2` | 2 | TBD |  |
## V4 API and Web Service

### V4.1 Generic Web Service Security

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-4.1.1` | 1 | Covered | `/api/update` requires form content type, HMAC signature, timestamp, nonce replay protection, client lookup, source authorization, and rate limit; see `vchat/views/api/views.py` and `tests/test_api_views.py`. |
| `v5.0.0-4.1.2` | 2 | Covered | The public web service surface is `/api/update`; it is registered through `SwaggerDocs` with an OpenAPI docstring schema and covered by request validation, auth, rate-limit, redirect, and size-limit tests in `tests/test_api_views.py`. |
| `v5.0.0-4.1.3` | 2 | Covered | API error responses use explicit 4xx/429 codes for bad content type, missing fields, signature/nonce/rate failures; see `tests/test_api_views.py`. |
### V4.2 HTTP Message Structure Validation

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-4.2.1` | 2 | Covered | `/api/update` rejects non-form content type and validates required fields, lengths, timestamp, nonce, and signature before mutation; see `vchat/views/api/views.py`. |
### V4.3 GraphQL

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-4.3.1` | 2 | N/A | No GraphQL endpoint or GraphQL dependency found in the application. |
| `v5.0.0-4.3.2` | 2 | N/A | No GraphQL endpoint or GraphQL dependency found in the application. |
### V4.4 WebSocket

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-4.4.1` | 1 | Covered | WebSocket path validates signed payload/chat context and checks provided `Origin` against configured origins before upgrade. |
| `v5.0.0-4.4.2` | 2 | Covered | WebSocket user payloads are parsed/validated through the chat flow and overlong messages are covered by regression tests. |
| `v5.0.0-4.4.3` | 2 | Covered | Chat actions use signed CSRF and WebSocket handshake origin is bound to configured application origins; invalid origins fail before `WebSocketResponse.prepare`. |
| `v5.0.0-4.4.4` | 2 | Covered | WebSocket/chat errors use user-safe messages with request IDs; regression tests cover guardrail and error flows. |
## V5 File Handling

### V5.1 File Handling Documentation

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-5.1.1` | 2 | TBD |  |
### V5.2 File Upload and Content

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-5.2.1` | 1 | TBD |  |
| `v5.0.0-5.2.2` | 1 | TBD |  |
| `v5.0.0-5.2.3` | 2 | TBD |  |
### V5.3 File Storage

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-5.3.1` | 1 | TBD |  |
| `v5.0.0-5.3.2` | 1 | TBD |  |
### V5.4 File Download

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-5.4.1` | 2 | TBD |  |
| `v5.0.0-5.4.2` | 2 | TBD |  |
| `v5.0.0-5.4.3` | 2 | TBD |  |
## V6 Authentication

### V6.1 Authentication Documentation

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-6.1.1` | 1 | TBD |  |
| `v5.0.0-6.1.2` | 2 | TBD |  |
| `v5.0.0-6.1.3` | 2 | TBD |  |
### V6.2 Password Security

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-6.2.1` | 1 | TBD |  |
| `v5.0.0-6.2.2` | 1 | TBD |  |
| `v5.0.0-6.2.3` | 1 | TBD |  |
| `v5.0.0-6.2.4` | 1 | TBD |  |
| `v5.0.0-6.2.5` | 1 | TBD |  |
| `v5.0.0-6.2.6` | 1 | TBD |  |
| `v5.0.0-6.2.7` | 1 | TBD |  |
| `v5.0.0-6.2.8` | 1 | TBD |  |
| `v5.0.0-6.2.9` | 2 | TBD |  |
| `v5.0.0-6.2.10` | 2 | TBD |  |
| `v5.0.0-6.2.11` | 2 | TBD |  |
| `v5.0.0-6.2.12` | 2 | TBD |  |
### V6.3 General Authentication Security

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-6.3.1` | 1 | TBD |  |
| `v5.0.0-6.3.2` | 1 | TBD |  |
| `v5.0.0-6.3.3` | 2 | TBD |  |
| `v5.0.0-6.3.4` | 2 | TBD |  |
### V6.4 Authentication Factor Lifecycle and Recovery

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-6.4.1` | 1 | TBD |  |
| `v5.0.0-6.4.2` | 1 | TBD |  |
| `v5.0.0-6.4.3` | 2 | TBD |  |
| `v5.0.0-6.4.4` | 2 | TBD |  |
### V6.5 General Multi-factor authentication requirements

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-6.5.1` | 2 | TBD |  |
| `v5.0.0-6.5.2` | 2 | TBD |  |
| `v5.0.0-6.5.3` | 2 | TBD |  |
| `v5.0.0-6.5.4` | 2 | TBD |  |
| `v5.0.0-6.5.5` | 2 | TBD |  |
### V6.6 Out-of-Band authentication mechanisms

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-6.6.1` | 2 | TBD |  |
| `v5.0.0-6.6.2` | 2 | TBD |  |
| `v5.0.0-6.6.3` | 2 | TBD |  |
### V6.8 Authentication with an Identity Provider

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-6.8.1` | 2 | TBD |  |
| `v5.0.0-6.8.2` | 2 | TBD |  |
| `v5.0.0-6.8.3` | 2 | TBD |  |
| `v5.0.0-6.8.4` | 2 | TBD |  |
## V7 Session Management

### V7.1 Session Management Documentation

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-7.1.1` | 2 | TBD |  |
| `v5.0.0-7.1.2` | 2 | TBD |  |
| `v5.0.0-7.1.3` | 2 | TBD |  |
### V7.2 Fundamental Session Management Security

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-7.2.1` | 1 | TBD |  |
| `v5.0.0-7.2.2` | 1 | TBD |  |
| `v5.0.0-7.2.3` | 1 | TBD |  |
| `v5.0.0-7.2.4` | 1 | TBD |  |
### V7.3 Session Timeout

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-7.3.1` | 2 | TBD |  |
| `v5.0.0-7.3.2` | 2 | TBD |  |
### V7.4 Session Termination

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-7.4.1` | 1 | TBD |  |
| `v5.0.0-7.4.2` | 1 | TBD |  |
| `v5.0.0-7.4.3` | 2 | TBD |  |
| `v5.0.0-7.4.4` | 2 | TBD |  |
| `v5.0.0-7.4.5` | 2 | TBD |  |
### V7.5 Defenses Against Session Abuse

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-7.5.1` | 2 | TBD |  |
| `v5.0.0-7.5.2` | 2 | TBD |  |
### V7.6 Federated Re-authentication

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-7.6.1` | 2 | TBD |  |
| `v5.0.0-7.6.2` | 2 | TBD |  |
## V8 Authorization

### V8.1 Authorization Documentation

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-8.1.1` | 1 | TBD |  |
| `v5.0.0-8.1.2` | 2 | TBD |  |
### V8.2 General Authorization Design

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-8.2.1` | 1 | TBD |  |
| `v5.0.0-8.2.2` | 1 | TBD |  |
| `v5.0.0-8.2.3` | 2 | TBD |  |
### V8.3 Operation Level Authorization

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-8.3.1` | 1 | TBD |  |
### V8.4 Other Authorization Considerations

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-8.4.1` | 2 | TBD |  |
## V9 Self-contained Tokens

### V9.1 Token source and integrity

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-9.1.1` | 1 | TBD |  |
| `v5.0.0-9.1.2` | 1 | TBD |  |
| `v5.0.0-9.1.3` | 1 | TBD |  |
### V9.2 Token content

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-9.2.1` | 1 | TBD |  |
| `v5.0.0-9.2.2` | 2 | TBD |  |
| `v5.0.0-9.2.3` | 2 | TBD |  |
| `v5.0.0-9.2.4` | 2 | TBD |  |
## V10 OAuth and OIDC

### V10.1 Generic OAuth and OIDC Security

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-10.1.1` | 2 | TBD |  |
| `v5.0.0-10.1.2` | 2 | TBD |  |
### V10.2 OAuth Client

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-10.2.1` | 2 | TBD |  |
| `v5.0.0-10.2.2` | 2 | TBD |  |
### V10.3 OAuth Resource Server

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-10.3.1` | 2 | N/A | Application does not expose an OAuth resource server API; `/api/update` uses HMAC, timestamp, nonce, and source scoping instead. |
| `v5.0.0-10.3.2` | 2 | N/A | Application does not expose an OAuth resource server API. |
| `v5.0.0-10.3.3` | 2 | N/A | Application does not expose an OAuth resource server API. |
| `v5.0.0-10.3.4` | 2 | N/A | Application does not expose an OAuth resource server API. |
### V10.4 OAuth Authorization Server

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-10.4.1` | 1 | N/A | Application is not an OAuth authorization server. |
| `v5.0.0-10.4.2` | 1 | N/A | Application is not an OAuth authorization server. |
| `v5.0.0-10.4.3` | 1 | N/A | Application is not an OAuth authorization server. |
| `v5.0.0-10.4.4` | 1 | N/A | Application is not an OAuth authorization server. |
| `v5.0.0-10.4.5` | 1 | N/A | Application is not an OAuth authorization server. |
| `v5.0.0-10.4.6` | 2 | N/A | Application is not an OAuth authorization server. |
| `v5.0.0-10.4.7` | 2 | N/A | Application is not an OAuth authorization server. |
| `v5.0.0-10.4.8` | 2 | N/A | Application is not an OAuth authorization server. |
| `v5.0.0-10.4.9` | 2 | N/A | Application is not an OAuth authorization server. |
| `v5.0.0-10.4.10` | 2 | N/A | Application is not an OAuth authorization server. |
| `v5.0.0-10.4.11` | 2 | N/A | Application is not an OAuth authorization server. |
### V10.5 OIDC Client

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-10.5.1` | 2 | N/A | Application does not implement OIDC client login; admin authentication is local/LDAP. |
| `v5.0.0-10.5.2` | 2 | N/A | Application does not implement OIDC client login. |
| `v5.0.0-10.5.3` | 2 | N/A | Application does not implement OIDC client login. |
| `v5.0.0-10.5.4` | 2 | N/A | Application does not implement OIDC client login. |
| `v5.0.0-10.5.5` | 2 | N/A | Application does not implement OIDC client login. |
### V10.6 OpenID Provider

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-10.6.1` | 2 | N/A | Application is not an OpenID Provider. |
| `v5.0.0-10.6.2` | 2 | N/A | Application is not an OpenID Provider. |
### V10.7 Consent Management

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-10.7.1` | 2 | N/A | OAuth/OIDC consent management is not implemented by this application. |
| `v5.0.0-10.7.2` | 2 | N/A | OAuth/OIDC consent management is not implemented by this application. |
| `v5.0.0-10.7.3` | 2 | N/A | OAuth/OIDC consent management is not implemented by this application. |
## V11 Cryptography

### V11.1 Cryptographic Inventory and Documentation

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-11.1.1` | 2 | TBD |  |
| `v5.0.0-11.1.2` | 2 | TBD |  |
### V11.2 Secure Cryptography Implementation

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-11.2.1` | 2 | TBD |  |
| `v5.0.0-11.2.2` | 2 | TBD |  |
| `v5.0.0-11.2.3` | 2 | TBD |  |
### V11.3 Encryption Algorithms

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-11.3.1` | 1 | TBD |  |
| `v5.0.0-11.3.2` | 1 | TBD |  |
| `v5.0.0-11.3.3` | 2 | TBD |  |
### V11.4 Hashing and Hash-based Functions

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-11.4.1` | 1 | TBD |  |
| `v5.0.0-11.4.2` | 2 | TBD |  |
| `v5.0.0-11.4.3` | 2 | TBD |  |
| `v5.0.0-11.4.4` | 2 | TBD |  |
### V11.5 Random Values

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-11.5.1` | 2 | TBD |  |
### V11.6 Public Key Cryptography

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-11.6.1` | 2 | TBD |  |
## V12 Secure Communication

### V12.1 General TLS Security Guidance

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-12.1.1` | 1 | TBD |  |
| `v5.0.0-12.1.2` | 2 | TBD |  |
| `v5.0.0-12.1.3` | 2 | TBD |  |
### V12.2 HTTPS Communication with External Facing Services

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-12.2.1` | 1 | TBD |  |
| `v5.0.0-12.2.2` | 1 | TBD |  |
### V12.3 General Service to Service Communication Security

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-12.3.1` | 2 | TBD |  |
| `v5.0.0-12.3.2` | 2 | TBD |  |
| `v5.0.0-12.3.3` | 2 | TBD |  |
| `v5.0.0-12.3.4` | 2 | TBD |  |
## V13 Configuration

### V13.1 Configuration Documentation

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-13.1.1` | 2 | TBD |  |
### V13.2 Backend Communication Configuration

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-13.2.1` | 2 | TBD |  |
| `v5.0.0-13.2.2` | 2 | TBD |  |
| `v5.0.0-13.2.3` | 2 | TBD |  |
| `v5.0.0-13.2.4` | 2 | TBD |  |
| `v5.0.0-13.2.5` | 2 | TBD |  |
### V13.3 Secret Management

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-13.3.1` | 2 | TBD |  |
| `v5.0.0-13.3.2` | 2 | TBD |  |
### V13.4 Unintended Information Leakage

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-13.4.1` | 1 | TBD |  |
| `v5.0.0-13.4.2` | 2 | TBD |  |
| `v5.0.0-13.4.3` | 2 | TBD |  |
| `v5.0.0-13.4.4` | 2 | TBD |  |
| `v5.0.0-13.4.5` | 2 | TBD |  |
## V14 Data Protection

### V14.1 Data Protection Documentation

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-14.1.1` | 2 | TBD |  |
| `v5.0.0-14.1.2` | 2 | TBD |  |
### V14.2 General Data Protection

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-14.2.1` | 1 | TBD |  |
| `v5.0.0-14.2.2` | 2 | TBD |  |
| `v5.0.0-14.2.3` | 2 | TBD |  |
| `v5.0.0-14.2.4` | 2 | TBD |  |
### V14.3 Client-side Data Protection

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-14.3.1` | 1 | TBD |  |
| `v5.0.0-14.3.2` | 2 | TBD |  |
| `v5.0.0-14.3.3` | 2 | TBD |  |
## V15 Secure Coding and Architecture

### V15.1 Secure Coding and Architecture Documentation

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-15.1.1` | 1 | TBD |  |
| `v5.0.0-15.1.2` | 2 | TBD |  |
| `v5.0.0-15.1.3` | 2 | TBD |  |
### V15.2 Security Architecture and Dependencies

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-15.2.1` | 1 | TBD |  |
| `v5.0.0-15.2.2` | 2 | TBD |  |
| `v5.0.0-15.2.3` | 2 | TBD |  |
### V15.3 Defensive Coding

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-15.3.1` | 1 | TBD |  |
| `v5.0.0-15.3.2` | 2 | TBD |  |
| `v5.0.0-15.3.3` | 2 | TBD |  |
| `v5.0.0-15.3.4` | 2 | TBD |  |
| `v5.0.0-15.3.5` | 2 | TBD |  |
| `v5.0.0-15.3.6` | 2 | TBD |  |
| `v5.0.0-15.3.7` | 2 | TBD |  |
## V16 Security Logging and Error Handling

### V16.1 Security Logging Documentation

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-16.1.1` | 2 | TBD |  |
### V16.2 General Logging

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-16.2.1` | 2 | TBD |  |
| `v5.0.0-16.2.2` | 2 | TBD |  |
| `v5.0.0-16.2.3` | 2 | TBD |  |
| `v5.0.0-16.2.4` | 2 | TBD |  |
| `v5.0.0-16.2.5` | 2 | TBD |  |
### V16.3 Security Events

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-16.3.1` | 2 | TBD |  |
| `v5.0.0-16.3.2` | 2 | TBD |  |
| `v5.0.0-16.3.3` | 2 | TBD |  |
| `v5.0.0-16.3.4` | 2 | TBD |  |
### V16.4 Log Protection

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-16.4.1` | 2 | TBD |  |
| `v5.0.0-16.4.2` | 2 | TBD |  |
| `v5.0.0-16.4.3` | 2 | TBD |  |
### V16.5 Error Handling

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-16.5.1` | 2 | TBD |  |
| `v5.0.0-16.5.2` | 2 | TBD |  |
| `v5.0.0-16.5.3` | 2 | TBD |  |
## V17 WebRTC

### V17.1 TURN Server

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-17.1.1` | 2 | N/A | No WebRTC/TURN/media/signaling feature in the application. |
### V17.2 Media

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-17.2.1` | 2 | N/A | No WebRTC/TURN/media/signaling feature in the application. |
| `v5.0.0-17.2.2` | 2 | N/A | No WebRTC/TURN/media/signaling feature in the application. |
| `v5.0.0-17.2.3` | 2 | N/A | No WebRTC/TURN/media/signaling feature in the application. |
| `v5.0.0-17.2.4` | 2 | N/A | No WebRTC/TURN/media/signaling feature in the application. |
### V17.3 Signaling

| ID | Level | Status | Evidence / notes |
| --- | ---: | --- | --- |
| `v5.0.0-17.3.1` | 2 | N/A | No WebRTC/TURN/media/signaling feature in the application. |
| `v5.0.0-17.3.2` | 2 | N/A | No WebRTC/TURN/media/signaling feature in the application. |
