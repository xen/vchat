# Design Knowledge

## Forms

- Save actions belong on the left side of form action rows unless a page-specific
  reason is documented.
- Reuse the existing primary button and form action layout before introducing a
  new placement.
- A new form page should match existing admin forms in spacing, label alignment,
  validation placement, and submit/cancel ordering.
- If one page differs from the established form pattern, treat it as a bug unless
  the user says the difference is intentional.

## UI consistency

- Prefer existing components and local HTML/CSS patterns over new one-off
  markup.
- Do not introduce a new visual pattern if a matching one exists in the current
  frontend.
- Check both the source template and rendered DOM before deciding a layout issue
  is fixed.

## Browser verification

- For local UI work, inspect the real rendered page in the Codex in-app browser.
- Do not rely on `curl`, guessed HTML, or source reading as the primary proof for
  visual layout, DOM structure, CSS, or browser behavior.
