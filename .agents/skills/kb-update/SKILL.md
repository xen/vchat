---
name: kb-update
description: >
  Maintain the project-local kb/ knowledge base after implementation. Use when the
  user says "/kb", "update KB", "capture this rule", "knowledge base", or asks to
  preserve a reusable project pattern without putting it in docs/.
---

Update project-local agent knowledge without bloating context.

## Workflow

1. Read `kb/index.md`.
2. Inspect `git diff --stat` and relevant changed files.
3. Identify reusable project knowledge:
   - repeated UI/design decisions,
   - architectural rules,
   - workflow or verification rituals,
   - hard invariants suitable for checks,
   - fixes the user has had to request more than once.
4. Propose the smallest KB edits needed.
5. Apply edits only when the user asked you to update KB, not when they asked only
   for a proposal.
6. Run `make agent-kb-check`.

## What belongs in KB

- Short directive rules that should steer future patches.
- Stable project structure and command rituals.
- Pointers from `kb/index.md` to focused KB files.
- Split recommendations when a file is growing too large.

## What does not belong in KB

- One-off debugging history.
- Long meeting notes or materialized conversations.
- Spec drafts, reports, or documents. Those belong in `docs/` only when the user
  asks to preserve them there.
- Server state snapshots unless they are stable operating policy.

## Style

- Keep entries compact and operational.
- Prefer bullets over paragraphs.
- Avoid rationale unless it changes future implementation choices.
- Do not read `docs/` unless the user pins a specific document.

## Splitting rule

If a KB file grows beyond roughly 200 lines, propose replacing it with a directory
that has an `index.md` plus focused child files. Preserve the old entry points by
updating `kb/index.md`.
