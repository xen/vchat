# Review Knowledge

## Purpose

`/review` is a compact code review ritual at the end of work. It should find
bugs, regressions, inconsistency with KB rules, and missing tests.

## Inputs

- Inspect `git diff` and changed files.
- Read `kb/index.md`, then only the KB files relevant to the diff.
- Do not read `docs/` unless the user pinned a document for the task.

## Output style

- Findings first, ordered by severity.
- Use exact file and line references.
- Keep comments terse: location, problem, fix.
- Answer reviews in Russian unless the user explicitly asks for another language.
- If there are no findings, say so and mention remaining test gaps or residual
  risk.

## Review scope

- Review only by default. Do not fix the code unless the user asks for fixes.
- Prefer project-specific consistency issues over generic style comments.
- Flag UI pattern drift, migration risks, swallowed errors, missing tests, and
  violations of fail-fast or server-access policy.
