# Frontend Knowledge

## Structure

- `frontend/` is the admin/front-office frontend.
- `frontend_chat/` is the embeddable chat widget frontend.
- Each frontend has its own `Makefile`, `package.json`, Vite config, source, and
  generated `dist/`.

## Build flow

- Use the frontend-local Makefile when changing a specific frontend.
- Use root `make frontend` when a change must rebuild both frontend packages.
- Keep generated assets in sync when the project convention requires committed
  `dist/` output.

## Verification

- For UI layout fixes, open local pages in the Codex in-app browser.
- Capture the relevant rendered state before changing templates or CSS when the
  issue is visual.
- Verify the changed page and at least one nearby existing page that uses the
  same pattern.
