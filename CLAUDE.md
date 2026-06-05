# vchat — Claude Code Instructions

## Git commits

**Never include `Co-Authored-By:` in commit messages.** Not in trailers, not in the body, nowhere. This applies to all commits regardless of who wrote the code.

Do not add any attribution lines (`Co-Authored-By`, `Signed-off-by` for AI, etc.) to commit messages.

## Code style

- No function names starting with `_` — inline or use plain names instead.

## Project environment

**This is an aiohttp project — there is no `manage.py`.**

- Python: `/Users/xen/Dev/sber/vchat/venv/bin/python` (Python 3.11.11, managed by uv)
- Activate venv: `. /Users/xen/Dev/sber/vchat/venv/bin/activate`
- All commands run from project root: `/Users/xen/Dev/sber/vchat/`
- Entry point: `entry.py` (not manage.py)
- ORM: SQLAlchemy async (`asyncpg` driver), sessions via `vchat.db.async_session_factory`
- Database: PostgreSQL at `postgresql://xen@localhost:5432/vchat`
- Migrations: `alembic upgrade head` (not migrate/makemigrations)
- Redis DB 30: app cache (`redis://localhost:6379/30`)
- Redis DB 31: Celery broker (`redis://localhost:6379/31`)
- Redis DB 32: Celery results backend (`redis://localhost:6379/32`)

### Running Python scripts

```bash
cd /Users/xen/Dev/sber/vchat
. venv/bin/activate && python myscript.py
```

Or without activating:
```bash
/Users/xen/Dev/sber/vchat/venv/bin/python myscript.py
```

### Quick DB queries (use psql directly, not Python shell)

```bash
psql postgresql://xen@localhost:5432/vchat -c "SELECT ..."
```

### Redis inspection

```bash
# Celery broker queue lengths
redis-cli -n 31 llen celery
redis-cli -n 31 llen crawler

# App cache
redis-cli -n 30 keys "*"

# Celery results
redis-cli -n 32 keys "*"
```

### Common make targets

| Target | Command |
|--------|---------|
| Run server | `make run` |
| Run celery | `make celery` |
| DB migrations | `make db` (alembic upgrade head) |
| New migration | `make revision` |
| Lint | `make lint` |
