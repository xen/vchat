# General rules

**Never include `Co-Authored-By:` in commit messages.** Not in trailers, not in the body, nowhere. This applies to all commits regardless of who wrote the code.

Do not add any attribution lines (`Co-Authored-By`, `Signed-off-by` for AI, etc.) to commit messages.

Always run Python tooling from the project virtualenv. Use `venv/bin/python`,
`venv/bin/pytest`, and other `venv/bin/...` entrypoints directly; do not try the
system `python`, `python3`, or global `pytest` first.

Do not use lazy imports for normal application dependencies. If code needs a
package, declare/fix the dependency and make the environment install it. `try:
import ... except ModuleNotFoundError` is forbidden except for local config
probes or OS/architecture-specific branches.

If a declared Python dependency is missing on a server, fix the environment and
any required system build dependencies instead of adding code-level fallbacks.

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

Current repo defaults in [vchat/config.yaml](/Users/xen/Dev/sber/vchat/vchat/config.yaml:19):

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
