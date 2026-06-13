# Security Review: vchat

## Scope

- Scan mode: repository-wide Codex Security scan for `/Users/xen/Dev/sber/vchat`.
- In scope: production/runtime code under `vchat/`, `jobs/`, `migrations/`, browser templates/assets, deployment manifests, Docker/compose assets, and dependency manifests.
- Explicitly excluded from primary production review: `venv/`, `htmlcov/`, `docs/`, generated coverage output, and test-only fixtures except where tests clarified expected controls.
- Threat model: generated during this scan from repository code and project KB; no remote server access was used.
- Validation mode: static source-to-sink tracing plus local dependency/config checks. Runtime web/database services were not started because the request was for vulnerability analysis, not fixes or live deployment testing.

### Scan Summary

| Field | Value |
|---|---|
| Reportable findings | 13 |
| Severity mix | high: 3, medium: 9, low: 1 |
| Confidence mix | high: 5, low: 1, medium: 7 |
| Coverage | 466 production/runtime files prioritized from rank input; 13 candidate ledgers; 10 reviewed surface groups |
| Artifacts | `/tmp/codex-security-scans/vchat/d409f5e_20260613T095105Z` |
| Markdown report | `/tmp/codex-security-scans/vchat/d409f5e_20260613T095105Z/report.md` |
| HTML report | `/tmp/codex-security-scans/vchat/d409f5e_20260613T095105Z/report.html` |

## Threat Model

# Overview

`vchat` is an aiohttp-based web application for a single-project chat/RAG system. It exposes authenticated administrative pages, public widget chat pages and websockets, a public HMAC-signed document update API, metrics and health endpoints, static/data file serving, and background Celery jobs for crawling, document extraction, indexing, embeddings, and trigger generation. Persistent assets include PostgreSQL records for users, chats, messages, sources, pages, chunks, API clients, widget integrations, triggers, crawl state, and admin events; Redis stores sessions, queues, rate-limit state, nonce state, active chat state, and Pub/Sub messages.

Primary runtime code lives under `vchat/` and `jobs/`. `migrations/` defines schema state and data-moving SQL. `frontend/`, `frontend_chat/`, `static/`, and `vchat/templates/` contain browser-delivered assets and templates. `tests/`, `htmlcov/`, `docs/`, and local tooling are not primary production surfaces unless deployed or exposed by configuration.

# Threat Model, Trust Boundaries, and Assumptions

Important assets and privileges:

- Authenticated admin access to users, sources, API clients, widgets, triggers, crawl/index actions, chat histories, documents, uploaded files, and project settings.
- API client secrets used to authenticate `/api/update` requests.
- Session cookies signed/encrypted with configured keys.
- LLM provider credentials, GigaChat/OpenAI/Yandex API keys, OAuth tokens, prompts, retrieved context, and generated answers.
- Indexed source documents, raw fetched content, chunks, embeddings, chat messages, and possible PII in RAG context.
- Redis and Celery queues that can trigger crawler, indexing, embedding, and trigger generation jobs.
- PostgreSQL integrity for source/page/chunk ownership and API-client-to-source authorization.

Main trust boundaries:

- Browser to aiohttp application: authenticated admin requests, CSRF-protected forms/actions, websocket chat messages, public widget chat payloads, and static/data file requests.
- External integrations to `/api/update`: unauthenticated network callers become trusted only after client id, HMAC signature, timestamp, nonce, source authorization, and rate-limit checks.
- Application to Redis/PostgreSQL/Celery: application code assumes local infrastructure is trusted and available; failures should surface loudly.
- Crawler/jobs to remote websites: source URLs, discovered links, downloaded HTML/documents, content types, redirects, and document bytes are attacker-controlled when the source website is attacker-controlled or compromised.
- Application/RAG to LLM providers: user chat text, retrieved snippets, widget prompts, source content, and provider responses cross a third-party model boundary.
- Admin/operator configuration to runtime: `vchat/config.yaml`, `local.yaml`, source settings, crawler rules, trigger rules, and widget settings are operator-controlled but can become high-impact if exposed to lower-privileged users.

Attacker-controlled inputs include login credentials, query strings, websocket/chat messages, widget codes, public API form bodies, HMAC request metadata, uploaded/file-related fields where exposed, source URLs, crawler-discovered URLs, remote response bodies, sitemaps, documents, LLM responses, browser Origin headers, and selected path parameters. Operator-controlled inputs include config files, environment-specific secrets, local deployment settings, project settings, source rules, and API client creation. Developer-controlled inputs include migrations, tests, build assets, and local scripts.

Assumptions:

- The production app is intended to sit behind HTTPS with secure cookies and a configured cookie domain.
- PostgreSQL, Redis, and Celery broker are trusted internal services, not directly reachable by arbitrary users.
- Authenticated admin pages are sensitive and should require a valid user session plus CSRF for state changes.
- Public widget chat is intentionally reachable without a normal admin session, but should be constrained to widget/source access and not leak admin-only documents or cross-widget chat history.
- Public `/api/update` is intentionally reachable by integration clients, but only signed clients should be able to trigger network fetches and indexing.

# Attack Surface, Mitigations, and Attacker Stories

Authentication and session management matter because admin routes control crawls, API clients, source settings, widgets, triggers, and user records. Relevant code includes `vchat/views/auth/views.py`, `vchat/middlewares/*`, `vchat/utils.py`, and templates/forms under `vchat/views/projects/`. Existing mitigations include WTForms CSRF for forms, signed CSRF helpers for action endpoints, session fixation avoidance via `new_session()`, password hashing with passlib, LDAP escaping, login delay/lock Redis keys, and secure-cookie configuration knobs.

Authorization and object scoping are high-impact for project/admin data. Since the app appears single-project, many endpoints rely on authenticated admin status rather than tenant isolation. Public widget and API endpoints must still avoid reaching arbitrary `Page`, `Chat`, `Source`, `WidgetIntegration`, or `TriggerResponseCache` rows across trust boundaries.

The public HMAC API in `vchat/views/api/views.py` is a critical surface. Existing mitigations include required form content type, timestamp TTL, nonce replay protection, per-client rate limits, encrypted stored client secrets, HMAC comparison, source host allow-listing, URL length limits, and raw-content size limits. Security review should focus on canonicalization gaps, redirect/SSRF behavior, host matching, client-source joins, replay/rate-limit keying, and content extraction side effects.

Crawler and document processing are broad untrusted-input surfaces. `jobs/crawler/*`, `jobs/documents/*`, and `jobs/indexing/*` ingest remote URLs, HTML, sitemaps, file content, MIME/content-type metadata, and discovered links. Relevant risks include SSRF, internal network reachability, unbounded redirects or downloads, path/file handling mistakes, parser denial of service, unsafe document extraction, URL normalization bypasses, and poisoned RAG content.

Chat, websocket, RAG, and LLM integrations in `vchat/views/chat/*` handle public/user text, JSON payloads, retrieved snippets, provider responses, trigger keys, and Redis Pub/Sub. Existing mitigations include structured payload parsing, snippet sanitization helpers, guardrail checks, signed trigger page IDs, chat/action scoping checks, request IDs, and token/response limits. Review should focus on prompt/data exfiltration, cross-chat authorization, unsafe rendering of model output, websocket CSRF/origin assumptions, and provider error/secret leakage.

Template rendering and static/data serving can create XSS or data exposure risks. Jinja autoescaping, explicit `html` escaping in some paths, and sanitization helpers reduce risk, but routes serving `/data/` with `follow_symlinks=True` and any templates rendering rich document/chat/model content deserve review.

Database/query safety matters where raw SQL appears in retrieval, migrations, and admin actions. SQLAlchemy parameterization is generally expected, while `sa.text()` and dynamically built SQL should be checked for attacker-controlled interpolation.

Observability surfaces `/metrics`, `/health/live`, `/health/ready`, logging, and admin event pages may expose operational details. Metrics collectors intentionally catch Redis errors for observability robustness; security relevance depends on whether metrics are public in deployment.

# Severity Calibration (Critical, High, Medium, Low)

Critical findings would allow unauthenticated remote code execution, arbitrary local file read through deployed static/data routes, extraction of configured API/LLM/session secrets, unauthenticated admin takeover, compromise of Redis/PostgreSQL through application inputs, or public API/widget access to arbitrary internal network resources with sensitive response exfiltration.

High findings would include authentication or CSRF bypass for admin state-changing actions, public API HMAC canonicalization bypass, API client source authorization bypass, websocket/chat cross-session data access, SSRF that reaches meaningful internal metadata or admin services, stored XSS in admin pages or public widgets, or crawler/document parser paths that allow persistent data poisoning with sensitive context leakage.

Medium findings would include limited reflected XSS in authenticated-only views, missing rate limits on costly public chat/API paths, weak origin checks on websockets where impact is constrained by signed payloads or session checks, leakage of operational metadata through metrics, oversized payload/parser denial of service within configured upload limits, or unsafe redirects that do not directly expose credentials.

Low findings would include hardening gaps in local/developer tooling, minor security header omissions, low-impact information disclosure in health/error messages, stale generated frontend/demo assets not deployed as sensitive surfaces, or issues requiring trusted operator control of `local.yaml`/config without a path from lower-privileged users.


## Findings

| Finding | Severity | Confidence | Category | Candidate |
|---|---|---|---|---|
| [Assistant Markdown renders unsanitized HTML into same-origin chat DOM](#1-assistant-markdown-renders-unsanitized-html-into-same-origin-chat-dom) | high | high | Cross-site scripting | `VCHAT-CHAT-MARKED-XSS-001` |
| [Disabled users retain access through existing sessions](#2-disabled-users-retain-access-through-existing-sessions) | high | high | Authentication state bypass | `AUTH-SESSION-INACTIVE-001` |
| [Signed update API can refetch an allowed URL and follow a changed redirect to internal hosts](#3-signed-update-api-can-refetch-an-allowed-url-and-follow-a-changed-redirect-to-internal-hosts) | high | medium | Server-side request forgery | `VCHAT-API-UPDATE-SSRF-REDIRECT-TOCTOU` |
| [Committed default signing and cookie keys can become live credentials outside Kubernetes](#4-committed-default-signing-and-cookie-keys-can-become-live-credentials-outside-kubernetes) | medium | medium | Hardcoded default secrets | `VCHAT-CONFIG-DEFAULT-SECRETS-001` |
| [Crawler and sitemap sync parse large remote bodies before project limits apply](#5-crawler-and-sitemap-sync-parse-large-remote-bodies-before-project-limits-apply) | medium | medium | Uncontrolled resource consumption | `VCHAT-CRAWLER-UNBOUNDED-DOWNLOAD-DOS-003` |
| [Crawler source origins can target private or loopback addresses](#6-crawler-source-origins-can-target-private-or-loopback-addresses) | medium | medium | Server-side request forgery | `VCHAT-CRAWLER-SOURCE-SSRF-001` |
| [Document CSV export does not neutralize spreadsheet formulas](#7-document-csv-export-does-not-neutralize-spreadsheet-formulas) | medium | high | CSV formula injection | `VCHAT-PROJ-002` |
| [File editor POST routes lack CSRF protection](#8-file-editor-post-routes-lack-csrf-protection) | medium | high | Cross-site request forgery | `VCHAT-PROJ-001` |
| [LDAP login reuses local users without enforcing inactive or LDAP-only account state](#9-ldap-login-reuses-local-users-without-enforcing-inactive-or-ldap-only-account-state) | medium | medium | Authentication state bypass | `AUTH-LDAP-LOCAL-STATE-002` |
| [Public widget chats retrieve from the global knowledge base without widget/source scoping](#10-public-widget-chats-retrieve-from-the-global-knowledge-base-without-widget-source-scoping) | medium | medium | Authorization bypass / data exposure | `VCHAT-PUB-RAG-GLOBAL-KB-001` |
| [Sitemap urlset entries can persist off-host page URLs under the current source](#11-sitemap-urlset-entries-can-persist-off-host-page-urls-under-the-current-source) | medium | low | Server-side request forgery | `VCHAT-CRAWLER-SITEMAP-OFFHOST-SSRF-002` |
| [Update API reads remote response bodies into memory before size limits apply](#12-update-api-reads-remote-response-bodies-into-memory-before-size-limits-apply) | medium | high | Uncontrolled resource consumption | `VCHAT-API-UPDATE-UNBOUNDED-READ-DOS` |
| [Public /data static mount can expose repository data artifacts and symlinks](#13-public-data-static-mount-can-expose-repository-data-artifacts-and-symlinks) | low | medium | Sensitive file exposure | `VCHAT-DATA-STATIC-EXPOSURE-001` |

### Confidence Scale

| Label | Meaning |
|---|---|
| high | Direct source, configuration, or runtime evidence supports the finding, with no material unresolved reachability or exploitability blocker. |
| medium | Source evidence supports a plausible issue, but runtime behavior, deployment configuration, role reachability, type constraints, or exploit reliability still need proof. |
| low | Weak or incomplete evidence; included here because the user requested a broad vulnerability analysis and the item has a concrete follow-up path. |

### [1] Assistant Markdown renders unsanitized HTML into same-origin chat DOM

| Field | Value |
|---|---|
| Severity | high |
| Confidence | high |
| Confidence rationale | Static source-to-sink trace plus dependency check show model output reaches marked.parse and innerHTML with no sanitizer. |
| Category | Cross-site scripting |
| CWE | CWE-79: Improper Neutralization of Input During Web Page Generation |
| Affected lines | vchat/templates/chat/chat.html:420-427; vchat/templates/chat/chat.html:453-455; vchat/templates/chat/chat.html:827-839; vchat/views/chat/views.py:1433-1454; vchat/views/chat/views.py:1563-1595 |

#### Summary

Assistant output is streamed, stored, replayed, parsed as Markdown, and assigned to innerHTML. Marked does not sanitize raw HTML, so prompt-influenced or RAG-poisoned assistant content can execute JavaScript in the vchat origin.

#### Validation

Validated by static sink review and local dependency behavior: marked.parse preserves raw HTML such as image event handlers. No DOMPurify, bleach, nh3, or CSP enforcement was found on this chat rendering path.

#### Dataflow

public or authenticated prompt / indexed content -> ai_chat_stream delta in vchat/views/chat/views.py -> total_content persisted in ChatMsg -> initialMessages replay or live websocket append -> marked.parse -> innerHTML.

#### Reachability

Unauthenticated widget users can influence assistant output; authenticated admins also open same-origin chat pages. A successful payload can run in the browser context that views the chat and can use same-origin fetches and DOM CSRF material visible in the page.

#### Severity

High because this is same-origin XSS in a privileged application surface with stored replay and public-widget influence. Severity would decrease if a strict CSP blocked script/event execution or if all assistant HTML were sanitized before rendering.

#### Remediation

Sanitize marked output with a proven HTML sanitizer before assigning innerHTML, or configure the renderer to escape raw HTML. Add regression tests for streamed and replayed assistant messages containing HTML event handlers, script tags, javascript URLs, and SVG payloads.

### [2] Disabled users retain access through existing sessions

| Field | Value |
|---|---|
| Severity | high |
| Confidence | high |
| Confidence rationale | Direct middleware and decorator trace shows inactive users are loaded into request state and login_required only checks presence. |
| Category | Authentication state bypass |
| CWE | CWE-287: Improper Authentication |
| Affected lines | vchat/middlewares/__init__.py:165-179; vchat/utils.py:297-309; vchat/views/auth/views.py:184-194 |

#### Summary

Password login blocks inactive users, but existing encrypted sessions remain valid after a user is disabled. The auth middleware constructs UserInfo with is_active=False and login_required still lets the request continue.

#### Validation

Validated by source trace. The only invalidation condition is a missing user row; is_active is read but not enforced in middleware or the common login_required decorator.

#### Dataflow

existing session cookie user_id -> auth_middleware select User.is_active -> request user set even when false -> login_required sees request user is not None -> protected admin view executes.

#### Reachability

A formerly valid user or stolen still-valid cookie can keep accessing admin routes until session expiry. In this project session_max_age defaults to 30 days.

#### Severity

High because account deactivation is a core access-control action and the bypass preserves admin access after revocation. Severity would decrease if sessions were very short-lived or every protected handler separately enforced is_active.

#### Remediation

Invalidate the session or reject the request when User.is_active is false in auth_middleware or login_required. Add tests for deactivating a logged-in user and then accessing representative admin routes.

### [3] Signed update API can refetch an allowed URL and follow a changed redirect to internal hosts

| Field | Value |
|---|---|
| Severity | high |
| Confidence | medium |
| Confidence rationale | The allowlist precheck and the actual content fetch are separate network requests; runtime redirect race behavior was not exercised against a live service. |
| Category | Server-side request forgery |
| CWE | CWE-918: Server-Side Request Forgery |
| Affected lines | vchat/views/api/views.py:533-538; vchat/views/api/views.py:552-555; vchat/views/api/views.py:256-271; vchat/views/api/views.py:573-601 |

#### Summary

The signed /api/update endpoint validates the original host and an initial redirect chain, then later calls a second fetch that follows redirects and does not verify the final consumed URL. An authorized API client controlling an allowed origin can change the second response to redirect to internal infrastructure.

#### Validation

Validated by code trace. _resolve_url_state performs one request and host checks that result, while _fetch_url_content later performs client.get with allow_redirects=True and reads the final response without checking the final host.

#### Dataflow

signed POST url -> update_document host allowlist -> _resolve_url_state redirect check -> _upsert_document -> _extract_content -> _fetch_url_content allow_redirects=True -> internal/forbidden response stored and indexed.

#### Reachability

Requires a valid API client secret and a Source host assigned to that client. The attacker needs control over the allowed host response or redirect behavior. The API route is public, HMAC-authenticated, and intended to be called externally.

#### Severity

High because it bypasses the endpoint SSRF control and can make the server fetch internal or cloud-local HTTP resources. Severity would drop if deployment egress policy blocks private/link-local destinations.

#### Remediation

Use one fetch for validation and consumption, or validate resp.url after redirects immediately before reading/storing. Disable redirects in the content fetch unless each hop is checked. Add tests with an allowed URL that redirects to loopback/private addresses on the consumed request.

### [4] Committed default signing and cookie keys can become live credentials outside Kubernetes

| Field | Value |
|---|---|
| Severity | medium |
| Confidence | medium |
| Confidence rationale | Defaults and consumers are clear; live exploitability depends on deployment overriding local.yaml or environment secrets. |
| Category | Hardcoded default secrets |
| CWE | CWE-798: Use of Hard-coded Credentials |
| Affected lines | vchat/config.yaml:4-5; vchat/config.yaml:111-113; vchat/settings.py:96-105; vchat/middlewares/__init__.py:265-273; vchat/utils.py:231-232; vchat/app.py:58 |

#### Summary

The repository ships concrete values for secret_key, cookie_key, and vchat_secret in the default production config. If a runtime misses local.yaml overrides, those committed values protect sessions, CSRF/chat/API tokens, and widget signatures.

#### Validation

Validated by config loading trace: config.yaml is always loaded first and local.yaml only overrides when present. Kubernetes base overrides key values from Secret env; compose content shown in the repo does not include all secrets.

#### Dataflow

vchat/config.yaml defaults -> settings._load_config merge -> session middleware encrypted cookie storage / itsdangerous serializers / project integration signatures.

#### Reachability

An attacker with repository access or leaked image contents can know defaults; exploitation requires a deployment using defaults or incomplete overrides.

#### Severity

Medium because the impact could be session/token forgery if defaults are live, but Kubernetes manifests show intended Secret overrides. Severity would rise if a deployed environment is confirmed to use these defaults.

#### Remediation

Remove real-looking defaults from committed config, require secrets from environment/local.yaml at startup, and fail fast if production mode uses default or missing key material. Add a startup check and deployment tests.

### [5] Crawler and sitemap sync parse large remote bodies before project limits apply

| Field | Value |
|---|---|
| Severity | medium |
| Confidence | medium |
| Confidence rationale | Source trace shows unbounded requests content and BeautifulSoup parse before the configured raw-content cap is effective. |
| Category | Uncontrolled resource consumption |
| CWE | CWE-400: Uncontrolled Resource Consumption |
| Affected lines | jobs/crawler/tasks.py:1959-1966; jobs/crawler/tasks.py:2026-2033; jobs/crawler/tasks.py:2374-2375; jobs/crawler/document_pipeline.py:807-815; vchat/config.yaml:11 |

#### Summary

Crawler sitemap fetches use requests.content and HTML extraction feeds full bodies into BeautifulSoup. Project raw-content limits protect later storage/parser paths, not network download and initial parse costs.

#### Validation

Validated by code review and project settings. The shard also checked that Scrapy DOWNLOAD_MAXSIZE is not reduced from the large default in project settings.

#### Dataflow

configured source/sitemap -> requests.get or Scrapy response -> full content/body in memory -> XML parsing or BeautifulSoup -> later size classification/storage cap.

#### Reachability

Requires a crawled source or sitemap controlled by an authenticated source manager, or compromise of an existing source. The result is worker memory/CPU pressure.

#### Severity

Medium because it can degrade crawler workers but requires source control and does not directly expose data. Severity would rise if public APIs can add crawl sources or if crawler workers share capacity with web serving.

#### Remediation

Set strict download max sizes/timeouts for Scrapy and requests, stream sitemap reads with byte caps, and reject oversized HTML before BeautifulSoup. Add tests around oversized sitemap and HTML responses.

### [6] Crawler source origins can target private or loopback addresses

| Field | Value |
|---|---|
| Severity | medium |
| Confidence | medium |
| Confidence rationale | Static trace shows no private-address rejection; exploitability depends on who can create or edit crawler Sources. |
| Category | Server-side request forgery |
| CWE | CWE-918: Server-Side Request Forgery |
| Affected lines | jobs/crawler/source_blocking.py:83-88; jobs/crawler/source_blocking.py:124-135; jobs/crawler/source_blocking.py:99-104; jobs/crawler/spiders/general.py:80-95 |

#### Summary

Crawler source validation resolves hostnames and fetches robots.txt/root pages, but it does not reject loopback, RFC1918, link-local, or metadata IPs. A source pointed at internal HTTP services can be fetched by the server network.

#### Validation

Validated by source review of source blocking, source normalization, Scrapy seed setup, and pipeline persistence. No network PoC was run because remote/internal server access was out of scope.

#### Dataflow

authenticated source URI -> check_source_blocking DNS/robots/root fetch -> crawler start URL/tracked host -> Scrapy requests -> document extraction/persistence.

#### Reachability

Requires an authenticated user with source-management privileges. The impact is server-side access to internal network resources and possible indexing of their responses.

#### Severity

Medium because the attacker already needs admin/source privileges, but the issue crosses from application configuration into server-network reachability. Severity would rise if less-privileged users can create sources or if cloud metadata/internal admin services are reachable.

#### Remediation

Reject private, loopback, link-local, multicast, and metadata destinations after DNS resolution and after redirects. Re-resolve before every outbound request or enforce egress ACLs at the network layer. Add tests for localhost, 127.0.0.1, ::1, 169.254.169.254, RFC1918, and DNS rebinding-like redirects.

### [7] Document CSV export does not neutralize spreadsheet formulas

| Field | Value |
|---|---|
| Severity | medium |
| Confidence | high |
| Confidence rationale | CSV writer receives attacker-influenced title, URI, and source strings directly; spreadsheet execution depends on the user opening the export. |
| Category | CSV formula injection |
| CWE | CWE-1236: Improper Neutralization of Formula Elements in a CSV File |
| Affected lines | vchat/views/projects/views.py:2910-2917; vchat/views/projects/views.py:2940-2977 |

#### Summary

The document export writes titles, URIs, and source labels directly to CSV. Values beginning with formula metacharacters can execute spreadsheet formulas when an admin opens the export in Excel/LibreOffice/Sheets.

#### Validation

Validated by source review of selected fields and csv.DictWriter usage. The attacker influence is through crawled/integration source metadata and page titles/URIs.

#### Dataflow

remote page/source metadata -> Page.title/Page.uri/Source.title/Source.uri -> project_documents_csv row -> csv.DictWriter.writerows -> spreadsheet application.

#### Reachability

Requires the attacker to influence indexed document metadata and the admin to export/open the CSV. The impact depends on spreadsheet security settings and user interaction.

#### Severity

Medium because this is a known data-export injection class with admin-targeted interaction, but it is not direct server compromise.

#### Remediation

Prefix formula-leading cells with a single quote or tab according to the chosen CSV policy, and include =, +, -, @, tab, and carriage return cases in tests.

### [8] File editor POST routes lack CSRF protection

| Field | Value |
|---|---|
| Severity | medium |
| Confidence | high |
| Confidence rationale | Routes and handlers accept browser form POSTs under login_required, and the reviewed forms do not include a CSRF token. |
| Category | Cross-site request forgery |
| CWE | CWE-352: Cross-Site Request Forgery |
| Affected lines | vchat/routes.py:74-82; vchat/views/projects/views.py:3616-3664; vchat/views/projects/views.py:3676-3729 |

#### Summary

The file editor supports create, save, delete, chunk deletion, and indexing-trigger side effects through normal form POSTs. Unlike most project action routes, these handlers are not HTMX-signed or WTForms-CSRF protected.

#### Validation

Validated by source review of route registration, handlers, and templates. Nearby project mutations were checked and rejected when CSRF controls were present.

#### Dataflow

cross-site form POST from authenticated browser -> /files or /file/{document_id} -> request.post -> create/update/delete Page -> delete Chunk -> schedule indexing.

#### Reachability

A malicious site can submit forms from a logged-in admin browser if cookies are sent. The affected actions can alter or delete indexed file documents.

#### Severity

Medium because the victim must already be authenticated and the affected surface is file content/indexing rather than credentials or user permissions. Severity would rise if these file documents feed public widget answers broadly.

#### Remediation

Add CSRF tokens to file forms and verify them on POST, or route mutations through the existing signed HTMX/WTForms CSRF pattern. Add tests for missing, invalid, and valid CSRF on create/save/delete actions.

### [9] LDAP login reuses local users without enforcing inactive or LDAP-only account state

| Field | Value |
|---|---|
| Severity | medium |
| Confidence | medium |
| Confidence rationale | Source trace is clear, but reachability depends on auth_ldap_enabled and LDAP identity mapping policy. |
| Category | Authentication state bypass |
| CWE | CWE-287: Improper Authentication |
| Affected lines | vchat/views/auth/views.py:276-300 |

#### Summary

After external LDAP authentication succeeds, the handler finds a local user by email and creates a session without checking is_active or is_ldap. This bypasses state checks that are enforced in the basic login path.

#### Validation

Validated by comparing basic login checks with LDAP login flow. The precondition is LDAP auth enabled and a matching LDAP identity email.

#### Dataflow

LDAP credentials/email -> authenticate_ldap success -> select User by normalized email -> existing User reused regardless of flags -> new_session -> session user_id set.

#### Reachability

A user who can authenticate to LDAP with a matching email can log into a locally inactive or non-LDAP account when LDAP mode is enabled.

#### Severity

Medium because it can bypass local account disablement or login-mode policy, but it requires successful LDAP authentication and deployment configuration.

#### Remediation

Before creating the session, enforce that an existing matched user is active and allowed for LDAP login, or define and implement explicit account-linking rules. Add tests for inactive existing users and non-LDAP local users through the LDAP login route.

### [10] Public widget chats retrieve from the global knowledge base without widget/source scoping

| Field | Value |
|---|---|
| Severity | medium |
| Confidence | medium |
| Confidence rationale | Source trace proves no widget/source filter in retrieval; product intent for single-project widgets remains an assumption. |
| Category | Authorization bypass / data exposure |
| CWE | CWE-862: Missing Authorization |
| Affected lines | vchat/views/projects/views.py:3505-3579; vchat/views/chat/views.py:1197-1229; vchat/views/chat/views.py:1411-1424; vchat/views/chat/ctx.py:619-638; vchat/views/chat/ctx.py:667-706 |

#### Summary

Public widget sessions are bound to a widget code, but RAG retrieval queries all global chunks. If widgets are intended to expose only selected sources, clients, or domains, any public widget can ask about all indexed documents.

#### Validation

Validated by tracing public widget creation, signed websocket payload validation, get_context invocation, and vector/fulltext SQL. The WidgetIntegration model evidence did not show source-scoping controls.

#### Dataflow

GET /widget/{code} -> signed websocket payload with widget code -> ws_chat validates widget exists -> get_context -> vector and full-text queries over all c.chat_id IS NULL page chunks.

#### Reachability

Unauthenticated users can open public widgets. The issue matters when not every indexed source is intended to be public through every widget.

#### Severity

Medium because it is a plausible cross-boundary data exposure, but the current repository is a single-project chatbot and may intentionally expose the whole KB to widgets. Severity would rise if different widgets map to different customers or source sets.

#### Remediation

Add explicit widget-to-source or widget-to-collection authorization and pass that scope into get_context. Filter vector and full-text retrieval by allowed source/page IDs, and test that two widgets cannot retrieve each other’s sources.

### [11] Sitemap urlset entries can persist off-host page URLs under the current source

| Field | Value |
|---|---|
| Severity | medium |
| Confidence | low |
| Confidence rationale | Static persistence path exists, but automatic off-host fetching depends on Scrapy middleware/runtime behavior that was not reproduced. |
| Category | Server-side request forgery |
| CWE | CWE-918: Server-Side Request Forgery |
| Affected lines | jobs/crawler/tasks.py:2321-2337; jobs/crawler/tasks.py:2374-2385 |

#### Summary

Sitemap files are checked against the source host, but URL entries inside a valid sitemap can name off-host pages. The shard found a fallback path that can associate those URLs with the current source.

#### Validation

Validated as plausible by static review. The main proof gap is whether normal queued offsite requests are filtered in every runtime path; direct page refresh remains a stronger follow-up path.

#### Dataflow

valid source sitemap -> _fetch_sitemap -> _parse_sitemap_document -> urlset loc entries -> upsert/prioritize page URLs under source -> possible later crawl/refresh.

#### Reachability

Requires control over a sitemap for an allowed source. If the offsite URL is later fetched, the crawler can reach attacker-selected hosts from the server network.

#### Severity

Medium/low confidence because the persistence problem is clear but automatic fetch reachability needs runtime confirmation. Severity would rise with a demonstrated queued fetch to a private address.

#### Remediation

Validate every urlset loc against the owning source host before persisting or prioritizing. Add tests for off-host, localhost, private IP, and redirecting sitemap entries.

### [12] Update API reads remote response bodies into memory before size limits apply

| Field | Value |
|---|---|
| Severity | medium |
| Confidence | high |
| Confidence rationale | Source trace directly shows await resp.read before downstream raw-content limits; no runtime memory measurement was needed for reportability. |
| Category | Uncontrolled resource consumption |
| CWE | CWE-400: Uncontrolled Resource Consumption |
| Affected lines | vchat/views/api/views.py:256-271; vchat/views/api/views.py:597-601; vchat/config.yaml:11 |

#### Summary

The HMAC-signed update API fetches remote page content using await resp.read, then decodes and stores/processes it. The configured raw_content_max_bytes cap is applied downstream after the full response has already been loaded.

#### Validation

Validated by static trace from update_document to _fetch_url_content. No streaming limit, Content-Length guard, or incremental read cap is present in the fetch helper.

#### Dataflow

signed /api/update URL -> _upsert_document -> _extract_content -> _fetch_url_content -> await resp.read loads full body -> downstream content handling.

#### Reachability

Requires an authorized API client and an allowed host that can serve a very large or slow response. The consequence is application worker memory/CPU exhaustion.

#### Severity

Medium because exploitation requires API-client access and an allowed origin, but it can degrade service availability. Severity would increase if API clients are broadly distributed or worker memory is tight.

#### Remediation

Enforce Content-Length and streamed read caps inside _fetch_url_content before decoding. Abort when the limit is exceeded and add tests with oversized and chunked responses.

### [13] Public /data static mount can expose repository data artifacts and symlinks

| Field | Value |
|---|---|
| Severity | low |
| Confidence | medium |
| Confidence rationale | The route and image copy behavior are direct evidence; current sampled data artifacts appear low sensitivity. |
| Category | Sensitive file exposure |
| CWE | CWE-200: Exposure of Sensitive Information to an Unauthorized Actor |
| Affected lines | vchat/routes.py:189-193; deploy/Dockerfile:43-45; deploy/Dockerfile.dockerignore:1-17 |

#### Summary

The app serves repository-level data/ at /data/ with follow_symlinks=True. The Docker build copies the whole context and the Docker ignore excludes local.yaml/docs/venv but not data/.

#### Validation

Validated by route/deploy review and local data directory sampling. No sensitive secret file was confirmed in data/ during this scan.

#### Dataflow

Docker COPY . . includes data/ -> aiohttp add_static /data/ follow_symlinks=True -> unauthenticated HTTP request can read files under data or symlink targets.

#### Reachability

Any web client can request /data paths if deployed. Impact depends on what artifacts are present in data/ or reachable through symlinks.

#### Severity

Low because current evidence shows reports/sitemap/loadtest artifacts rather than secrets, but follow_symlinks and deployment copy behavior make future sensitive exposure plausible.

#### Remediation

Do not mount repository data/ publicly by default. Remove follow_symlinks unless needed, exclude data/ from Docker build context, and serve only an explicit allowlisted media/export directory.

## Reviewed Surfaces

| Surface | Risk Area | Outcome | Notes |
|---|---|---|---|
| Public chat and widget rendering | XSS / RAG data scope | Reported | Unsanitized assistant Markdown and global KB retrieval survived validation. |
| Signed update API | SSRF / resource exhaustion | Reported | Redirect TOCTOU SSRF and unbounded body reads survived validation. |
| Authentication and sessions | Revocation / LDAP account state | Reported | Inactive-session and LDAP local-state bypasses survived validation. |
| Crawler and sitemap jobs | SSRF / DoS | Reported | Private-origin crawler SSRF, sitemap off-host SSRF follow-up, and large-body DoS survived with calibrated confidence. |
| Project file editor and CSV export | CSRF / CSV injection | Reported | File editor POSTs lack CSRF; CSV export writes formula-capable cells. |
| Configuration and deploy assets | Default secrets / static data exposure | Reported | Default key material and /data static serving are precondition-dependent findings. |
| Project action routes | CSRF | Rejected | HTMX signed CSRF, WTForms CSRF, or explicit signed-CSRF checks were found on reviewed mutation routes outside file editor. |
| Metrics exposure | Information disclosure | Rejected | Kubernetes ingress blackholes /metrics while ServiceMonitor uses internal ClusterIP. |
| Dependency and container hardening | Supply chain / container escape | Rejected | Compiled lock files are hash-pinned; runtime runs non-root with read-only filesystem, dropped caps, seccomp, and no service-account token automount. |
| SQL query construction | SQL injection | No issue found | Reviewed high-risk raw sa.text queries used bound parameters for attacker-controlled data in inspected paths. |

## Open Questions And Follow Up

- Confirm whether public widgets are intended to access the whole global KB. If not, run a focused fix review around `vchat/views/chat/ctx.py` and `WidgetIntegration` source scoping.
- Reproduce `VCHAT-CRAWLER-SITEMAP-OFFHOST-SSRF-002` with a local disposable Scrapy run to decide whether it should be raised from low-confidence medium to high-confidence medium/high.
- Check the live deployment config for default `secret_key`, `cookie_key`, and `vchat_secret` usage before prioritizing key rotation. This scan did not access remote servers by policy.
- Run a dependency advisory scan with `pip-audit` or the project-approved equivalent after installing it in the project virtualenv. The tool was not installed during this scan.

## Artifact Index

- Threat model: `/tmp/codex-security-scans/vchat/d409f5e_20260613T095105Z/artifacts/01_context/threat_model.md`
- Raw candidates: `/tmp/codex-security-scans/vchat/d409f5e_20260613T095105Z/artifacts/02_discovery/raw_candidates.jsonl`
- Work ledger: `/tmp/codex-security-scans/vchat/d409f5e_20260613T095105Z/artifacts/02_discovery/work_ledger.jsonl`
- Reviewed surfaces: `/tmp/codex-security-scans/vchat/d409f5e_20260613T095105Z/artifacts/03_coverage/reviewed_surfaces.md`
- Validation summary: `/tmp/codex-security-scans/vchat/d409f5e_20260613T095105Z/artifacts/04_reconciliation/validation_summary.md`
- Attack-path summary: `/tmp/codex-security-scans/vchat/d409f5e_20260613T095105Z/artifacts/04_reconciliation/attack_path_report.md`
