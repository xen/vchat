# Operations Knowledge

## Server access

- Do not access any remote server unless the user explicitly asks for server
  access in the current task.
- Server details in `AGENTS.md` are reference material, not standing permission.
- If server access would help but was not requested, ask first and wait.

## Local database

- Local PostgreSQL connection: `postgresql://xen@localhost:5432/vchat`.
- For migrations, verify relevant record counts before and after when data
  integrity is part of the task.

## Redis

- Local repo defaults use separate Redis databases for app, Celery broker, and
  Celery result backend.
- Do not mix local defaults with current test-server runtime overrides.
- Queue diagnostics must read from the Celery broker DB, not the app Redis DB.

## Deploy

- Prefer existing deployment scripts and Makefile targets.
- Do not run deployment, SSH, remote psql, remote Redis, systemd, or journal
  commands without explicit current-task permission.
