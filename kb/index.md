# Agent Knowledge Base

This directory is the project-local knowledge base for agents.

Start here when the user asks to use project knowledge, `/kb`, `/review`, or
when a task touches a repeated project pattern.

Do not read `docs/` unless the user pins a specific document or asks to work
with documents. `docs/` is a materialized conversation/document archive, not
agent operating knowledge.

## Map

- `kb/workflow.md` - task rituals, when to read KB, when to update it.
- `kb/design.md` - UI taste, layout decisions, repeated visual patterns.
- `kb/frontend.md` - frontend structure, local browser verification, build flow.
- `kb/backend.md` - backend layout, migrations, jobs, config, fail-fast rules.
- `kb/ops.md` - server access, local database, Redis and deploy boundaries.
- `kb/review.md` - end-of-task review ritual and review output style.
- `kb/invariants.md` - hard rules suitable for checks, hooks, and CI.

## Maintenance

Keep files short and directive. Prefer rules that affect future patches over
history or rationale.

If a file grows beyond roughly 200 lines, split it into a directory with an
`index.md` and focused child files. Example: replace the single design file with
a design directory containing an index, forms notes, and navigation notes.
