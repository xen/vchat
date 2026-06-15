# Project Invariants

These rules should be enforced by hooks, checks, or review whenever practical.

## Commit messages

- Never include `Co-Authored-By:`.
- Do not add AI attribution trailers.

## Python

- Python tooling must run through `venv/bin/...`.
- Normal application dependencies must not use lazy import fallbacks.
- Missing declared dependencies are environment problems, not code fallback
  opportunities.
- Do not use `_` as a scratch or ignored variable name outside i18n/gettext
  contexts. Use a visible `_ignore_*` or `_skip_*` name, or omit the assignment.
- Runtime code must not use `assert` for ordinary validation or type narrowing.
- Type-checker fixes should not introduce impossible runtime branches.

## Architecture and operations

- No remote server access without explicit current-task permission.
- Default to fail-fast behavior.
- Do not preserve legacy call paths or compatibility shims without explicit user
  approval.
- User-provided or model-provided text must not be inserted with `innerHTML`
  unless it first passes through a project allowlist renderer such as
  `renderSafeAssistantMarkdown`, or is escaped.
- Browser form POST mutations must include signed CSRF tokens. HTML forms carry
  `csrf_token`; fetch/HTMX requests use `X-CSRFToken`.
- Public widgets must never query the global knowledge base without explicit
  source scoping.

## Knowledge base

- `docs/` is not agent operating knowledge.
- `kb/index.md` must point to existing KB files.
- KB files should stay short. Split files that grow beyond the agreed threshold.
