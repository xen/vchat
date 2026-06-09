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
- Do not add code-level dependency fallbacks for missing declared packages.
- Keep environment-specific runtime differences documented in KB or AGENTS
  policy, not hidden in application branches.

## Failure policy

- Default to fail-fast behavior.
- Do not hide database, Redis, Celery, network, or dependency failures behind
  silent recovery.
- Do not add backward-compatibility shims, deprecated task aliases, or old call
  paths unless the user explicitly approves that exact tradeoff.

## Python tooling

- Run Python tooling from the project virtualenv using `venv/bin/...`.
- Do not try system `python`, `python3`, or global `pytest` first.
