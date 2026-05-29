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
