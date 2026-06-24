# Plan-and-Validate working docs

This directory backs the **Plan-and-Validate Workflow** in `AGENTS.md`. For
non-trivial work, agents follow `PLAN_EXECUTION_PROTOCOL.md`: save a
`<TOPIC>_PLAN.md` before coding, execute against it, and record a
`<TOPIC>_VALIDATION.md` afterward.

Those generated `<TOPIC>_PLAN.md` / `<TOPIC>_VALIDATION.md` files are **local
working artifacts and are intentionally gitignored** — they accumulated to 600+
tracked files (one PLAN+VALIDATION pair per task, including every
review-comment fix) before this folder was ignored. The planning *discipline* is
mandatory; committing the files is not. Summarize the plan and validation
outcome in the PR description and commit messages instead. Do not `git add -f`
the generated docs.

Only two files in `plans/` are tracked:

- `PLAN_EXECUTION_PROTOCOL.md` — the protocol definition itself.
- `README.md` — this file.

This mirrors `docs/awf-plans/`, which stores the control-plane's per-workspace
plan/conformance artifacts and is likewise gitignored except for its README.
