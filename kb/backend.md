# Backend Knowledge

## Structure

- `vchat/` contains the web application, settings, routes, views, templates,
  database model definitions, metrics, and middleware.
- `jobs/` contains background execution: Celery wiring, crawler, embedder,
  indexing, documents, and triggers.
- `migrations/` contains Alembic migrations and must stay consistent with model
  changes.
- `tests/` contains unit, integration-style, chat, crawler, and RAG quality
  tests.

## Configuration

- Prefer the existing config system and `vchat/config.yaml` defaults.
- Config keys are flat lowercase internally. Environment overrides may arrive
  in any case, but must normalize to the same lowercase key name; do not add
  aliases for the same setting.
- In production mode, fail fast if `secret_key`, `cookie_key`, or
  `vchat_secret` remain empty, placeholders, or unchanged from defaults.
- Do not add code-level dependency fallbacks for missing declared packages.
- Keep environment-specific runtime differences documented in KB or AGENTS
  policy, not hidden in application branches.

## Crawler and remote input

- Enforce remote body limits while streaming and before handing data to
  parsers. Do not download an entire response and then check its size; the limit
  must stop memory growth and parser work at the boundary. New crawler-style
  downloads should use the shared configured byte limit rather than local magic
  numbers.

## RAG and widgets

- Public widgets are untrusted external entry points, so RAG retrieval must be
  scoped by explicit widget/source configuration. A missing binding is a
  configuration problem, not permission to search the whole project knowledge
  base. Future widget features should keep this default-deny posture for any
  project data they expose.

## Failure policy

- Default to fail-fast behavior.
- Do not hide database, Redis, Celery, network, or dependency failures behind
  silent recovery.
- Do not add backward-compatibility shims, deprecated task aliases, or old call
  paths unless the user explicitly approves that exact tradeoff.
- Do not add runtime guards, assertions, or exceptions only to satisfy a type
  checker. Restructure the control flow or use a narrow local type annotation.

## Python tooling

- Run Python tooling from the project virtualenv using `venv/bin/...`.
- Do not try system `python`, `python3`, or global `pytest` first.
