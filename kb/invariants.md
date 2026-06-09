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

## Architecture and operations

- No remote server access without explicit current-task permission.
- Default to fail-fast behavior.
- Do not preserve legacy call paths or compatibility shims without explicit user
  approval.

## Knowledge base

- `docs/` is not agent operating knowledge.
- `kb/index.md` must point to existing KB files.
- KB files should stay short. Split files that grow beyond the agreed threshold.
