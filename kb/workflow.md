# Workflow Knowledge

## Project memory boundaries

- `kb/` is agent operating memory.
- `docs/` is a document archive created during human-agent work.
- Read `docs/` only when the user names or pins a specific document, or when a
  KB file explicitly points to a pinned document for the current task.

## Task flow

- Start with local code and `kb/index.md` when the task matches an existing
  pattern.
- Prefer existing Makefile targets and project scripts over ad hoc commands.
- Use `venv/bin/...` for Python tooling.
- Do not access remote servers unless the user explicitly asks for server
  access in the current task.

## End-of-task rituals

- Use `/review` when a task changes code or behavior and the user wants the
  compact review pass.
- Use `/kb` after a task produces a reusable project rule, design decision, or
  repeated fix.
- Do not add one-off incident details to KB. Capture only knowledge that should
  steer future work.

## KB update rule

KB changes should be small, quoted, and operational:

- bad: long history of why the rule exists.
- good: "Save actions belong on the left side of form action rows unless a
  page-specific reason is documented."
