# Actions Knowledge

Use this for small page commands. For full page forms and WTForms validation,
read `kb/forms.md`.

## Boundary

- Actions are independent commands already located on a page.
- Actions do not own a page, route template, or full form state.
- Use actions for commands like starting a background task, resetting a token,
  deleting an item, clearing generated data, or toggling status.
- If a command edits many fields or owns page/modal state, it is a page form;
  implement it using `kb/forms.md`.

Examples:

- `generate_triggers` starts trigger generation in the background.
- `clear_triggers` clears generated triggers and response caches.
- `widget_reset_code` resets a widget embed code.
- `widget_delete` deletes a widget integration.

Not actions:

- `project_integration` add form.
- `project_widget_edit` full edit form.
- `project_triggers` settings form.

## Routes

- Actions use the shared route:
  `/actions/{action}/{item_id}`.
- Do not create named standalone routes for commands with no page and no
  template.
- Do not use the shared action route for a page form.

## HTMX

- HTMX actions may use `hx-post="{{ url('actions', ...) }}"`.
- Rely on the global page CSRF header from `<body>`.
- Do not add per-button `hx-headers`.
- Do not generate `csrf_token()|tojson` near individual HTMX buttons.
- Use `type="button"` for standalone HTMX buttons that do not submit a form.
- Keep action markup separate from the main page form.

Current button shape:

```jinja
<button
  type="button"
  hx-post="{{ url('actions', action='generate_triggers', item_id='global') }}"
  hx-swap="none"
>
```

## Responses

- Actions return concrete action responses: `web.Response(text="ok")`,
  `HX-Trigger`, `HX-Refresh`, or an error status.
- Do not redirect from actions.
- Use a named `HX-Trigger` when the page has specific refresh behavior.

Current response shape:

```python
response = web.Response(text="ok")
response.headers["HX-Trigger"] = "project-triggers:refresh"
return response
```

## Data and Jobs

- Actions may parse only minimal command input such as `item_id`.
- Actions must not parse or sanitize fields owned by page forms.
- Complex user input means the command should become a page form.
- Background-job buttons are actions: enqueue the job and return immediately.
- Keep fail-fast behavior for database, Redis, Celery, network, and dependency
  failures.
- Do not keep legacy action aliases unless explicitly approved.

## Checks

- Test actions separately from page forms.
- Assert `HX-Trigger` or `HX-Refresh` headers and side effects.
- Template tests should assert `url('actions', ...)` and absence of per-button
  `hx-headers`.
- Page-form tests should assert redirects from page views, not actions.

