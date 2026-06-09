# General rules

**Never include `Co-Authored-By:` in commit messages.** Not in trailers, not in the body, nowhere. This applies to all commits regardless of who wrote the code.

Do not add any attribution lines (`Co-Authored-By`, `Signed-off-by` for AI, etc.) to commit messages.

# Agent Knowledge

Use [kb/index.md](kb/index.md) as the entry point for project-local agent
knowledge. Read only the KB files relevant to the task.

Do not treat `docs/` as agent operating knowledge. `docs/` is a materialized
conversation/document archive used when the user asks to create, preserve, or
work through documents. Read files from `docs/` only when the user explicitly
pins a specific document or asks to work with documents.

Always run Python tooling from the project virtualenv. Use `venv/bin/python`,
`venv/bin/pytest`, and other `venv/bin/...` entrypoints directly; do not try the
system `python`, `python3`, or global `pytest` first.

Do not use lazy imports for normal application dependencies. If code needs a
package, declare/fix the dependency and make the environment install it. `try:
import ... except ModuleNotFoundError` is forbidden except for local config
probes or OS/architecture-specific branches.

If a declared Python dependency is missing on a server, fix the environment and
any required system build dependencies instead of adding code-level fallbacks.

## Error Handling / Legacy Policy

Default to fail-fast behavior.

- Do not add fallback logic, graceful degradation, tolerant signatures, silent
  retries, or backward-compatibility shims unless I explicitly ask for them.
- If the database, Redis, Celery, network, or another dependency is broken, let
  the code fail loudly with a full traceback.
- Do not hide infrastructure or data problems behind `try/except` unless the
  exception is re-raised or I explicitly asked for a recovery path.
- Prefer deleting obsolete code over preserving legacy entrypoints “just in
  case”.
- Do not keep deprecated task names, old call paths, compatibility wrappers, or
  migration-era glue unless I explicitly approved that exact tradeoff.
- Every time you think a legacy/backward-compatibility layer might be needed,
  stop and ask me a direct question first instead of implementing it.

# Server Access Policy

Do not access any remote server unless the user explicitly asks for server
access in the current task. This includes SSH, remote psql, remote Redis,
systemd, journalctl, deployment checks, and any command targeting
`cdn.okumy.com` or another non-local host.

The server details below are reference material only. They are not permission
to run server commands while investigating unrelated issues, local failures,
tests, migrations, UI work, or diagnostics.

If server access might be useful but was not explicitly requested, ask for
permission first and wait for confirmation.

# Test Server Database Access

**Server:** `deploy@cdn.okumy.com` (тестовый сервер)
**Root access:** `root@cdn.okumy.com`
**Project path:** `/var/www/vchat`
**Database:** `vchat`
**User:** `deploy`

Direct psql connection:

```bash
ssh deploy@cdn.okumy.com "psql -U deploy -d vchat -c 'YOUR_SQL_QUERY'"
```

# Test Server Runtime Notes

`vchat` runs under deploy's user-level systemd units:

- `vchat-backend.service`
- `vchat-celery.service`
- `vchat-embedder.service`

Useful checks:

```bash
ssh deploy@cdn.okumy.com "systemctl --user status vchat-backend.service vchat-celery.service vchat-embedder.service --no-pager"
ssh deploy@cdn.okumy.com "journalctl --user -u vchat-embedder.service -n 200 --no-pager"
ssh deploy@cdn.okumy.com "psql -U deploy -d vchat -c \"select count(*) from chunk where embedding is null;\""
```

## Redis Notes

Do not mix project defaults with the current test server runtime.

### Repo / local default namespace

Current repo defaults in [vchat/config.yaml](vchat/config.yaml):

- App Redis: `redis://localhost:6379/30`
- Celery broker Redis: `redis://localhost:6379/31`
- Celery result backend Redis: `redis://localhost:6379/32`

This matches the project convention where local Redis DBs are split by tens /
adjacent slots for each project runtime.

### Current test server runtime override

The current `deploy@cdn.okumy.com` runtime is not using the repo-default Celery
DBs. The live worker processes currently point to:

- App Redis: `redis://localhost:6379/30`
- Celery broker Redis: `redis://localhost:6379/14`
- Celery result backend Redis: `redis://localhost:6379/15`

Treat `14/15` as a server-specific current-state fact, not as the project
default.

For queue diagnostics on the current test server runtime:

```bash
ssh deploy@cdn.okumy.com "redis-cli -n 14 LLEN embeddings"
ssh deploy@cdn.okumy.com "redis-cli -n 14 LLEN crawler"
ssh deploy@cdn.okumy.com "redis-cli -n 14 --scan"
```

Important:

- `embeddings` is the embedder queue name.
- `crawler` is the crawler queue name.
- `celery` may be empty even when the system is healthy.
- Prometheus queue gauges must read from the Celery broker DB, not the app Redis DB.

# Local Database

**Connection:** `postgresql://xen@localhost:5432/vchat`

- Always verify record counts before and after migration to ensure data integrity.

# Local UI / Browser Rules

- For any `local.vchat.com`, `localhost`, or other local UI link, always open and inspect it only in the Codex in-app browser. Do not rely on `curl`, guessed HTML, or external browser state as the primary verification path when the task is about visual layout, DOM structure, CSS, or browser behavior.
- Before changing templates or CSS to fix a visible UI issue, first confirm the real rendered DOM/state in the in-app browser. If the page cannot be inspected there because of auth, broken browser attachment, or another blocker, state that explicitly before making speculative UI changes.
