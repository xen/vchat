# General rules

**Never include `Co-Authored-By:` in commit messages.** Not in trailers, not in the body, nowhere. This applies to all commits regardless of who wrote the code.

Do not add any attribution lines (`Co-Authored-By`, `Signed-off-by` for AI, etc.) to commit messages.

# Test Server Database Access

**Server:** `deploy@cdn.okumy.com` (тестовый сервер)
**Database:** `vchat`
**User:** `deploy`

Direct psql connection:

```bash
ssh deploy@cdn.okumy.com "psql -U deploy -d vchat -c 'YOUR_SQL_QUERY'"
```

# Local Database

**Connection:** `postgresql://xen@localhost:5432/vchat`

- Always verify record counts before and after migration to ensure data integrity.
