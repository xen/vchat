# Forms Knowledge

Use this for admin page forms. For standalone commands and HTMX buttons, read
`kb/actions.md`.

## Boundary

- Page forms own page or modal state and are served by the page view at their
  own URL.
- Page forms POST normally with `method="POST"` and include
  `{{ form.csrf_token }}`.
- Page forms may redirect after valid POST. Actions must not redirect as a page
  navigation shortcut.
- If a command has no page, no template, and no form state, it is an action; see
  `kb/actions.md`.

Examples:

- `project_integration` is the `/integration` add form for `WidgetIntegration`.
- `project_widget_edit` is the full `/integration/{widget_id}` edit form.
- `project_triggers` is the `/triggers` settings form.

## Naming

- Add/create forms are named `[Thing]Add`.
- Edit/update forms are named `[Thing]Edit`.
- Nested subforms may use domain names such as `PinnedMessageForm`.
- Do not use generic names like `[Thing]Form` for add/edit forms.
- Keep simple field declarations local to each form class. Do not extract a
  one-use helper just to share a short WTForms field between add/edit classes.
- Do not add form properties named `default_*`.

## View Contract

- Construct the form once near the start of the page view.
- For GET, pass `data={...}` with render-ready values; normalize empty model
  values to domain constants there.
- For POST, pass `formdata=await request.post()` and validate once.
- On invalid POST, rollback and render the same template with status `400`.
- On valid POST, assign form-cleaned values to the model, commit, emit
  event/flash if needed, and redirect from the page view.
- Keep context narrow. Widget edit uses only `project` and `form`.
- Views may assign `form.field.data` and explicit `form.cleaned_*` values.
  Views must not parse raw POST fields owned by the form.

## Validation and Cleaning

- Sanitize on write inside the WTForms class, usually in `validate_<field>`.
- Do not sanitize display-time values to compensate for dirty saved data.
- Do not silently truncate user input. Validate length and return form errors.
- Let WTForms field validators handle ordinary required/length/choice checks.
- Rich text uses `SafeHTML.clean(value, max_text_length=...)`: it returns clean
  HTML or raises `ValidationError`.
- Plain text list entries should be stripped and saved as plain strings.
- Empty cleaned lists fall back to domain constants inside the form.
- `FieldList.data` is computed by WTForms; do not assign it. Use
  `cleaned_welcome_messages`, `cleaned_waiting_messages`, or
  `cleaned_pinned_messages` for aggregate cleaned values.

## WTForms Lists

- Use `FieldList(StringField(...))` for dynamic string lists.
- Use `FieldList(FormField(SubForm))` for dynamic structured lists.
- Do not invent custom fields for ordinary dynamic lists.
- Client names must match WTForms indexing:
  - `welcome_messages-0`
  - `waiting_messages-0`
  - `pinned_messages-0-text`
  - `pinned_messages-0-color`
- Do not use legacy names like `welcome_text[]`, `waiting_text[]`,
  `pinned_text[]`, or `pinned_color[]`.
- Dynamic JavaScript must reindex rows before submit and after add/remove/drag.
- Put field-level constraints on the field itself. Example: pinned color is a
  `SelectField`, not a manual allowlist check in `validate_pinned_messages`.

## Templates

- If a page has one main form, the context variable is `form`.
- Add modals may use a specific variable such as `create_form`.
- Use `vchat/templates/macros.html` `render_field` for ordinary fields.
- Custom rich editors may use hidden inputs, but hidden input names must be
  WTForms field names.
- Do not put value fallbacks in templates, for example
  `{{ form.footer_text.data or default_widget_footer_text }}`.
- A full page form must not use `hx-post`, `hx-swap="none"`, or per-element
  `hx-headers`.
- HTMX buttons inside a page are actions; see `kb/actions.md`.

## Defaults

- Reused widget defaults are domain constants:
  - `WIDGET_AGENT_NAME`
  - `WIDGET_FOOTER_TEXT`
  - `WIDGET_WELCOME_MESSAGES`
  - `WIDGET_WAITING_MESSAGES`
  - `WIDGET_ALLOWED_PINNED_COLORS`
- Large prompt defaults may remain module constants such as
  `DEFAULT_SYSTEM_PROMPT` and `DEFAULT_SUGGESTIONS_PROMPT`.
- Add forms use field `default=...` for editable default values.
- Add views explicitly assign model fields that are not present in the add form.
- Edit views fill GET `data={...}` with current model values or domain defaults;
  templates render form values directly.

## Checks

- Test forms directly with `MultiDict`.
- POST tests must use WTForms field names.
- Template tests should assert rendered field names.
- Assert rich text keeps allowed tags/links and removes scripts/unsafe links.
- Assert long text is rejected, not truncated.
- Assert page-form redirects happen only from page views, not actions.
