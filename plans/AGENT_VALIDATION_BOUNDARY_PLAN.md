# Agent Validation Boundary Plan

## Problem Statement

AWF dogfood agents interpreted "rely on `.awf/workspace.yml` for validation" as
permission to run the full AWF validation suite inside the agent phase. That is
the wrong ownership boundary: agents should write code, add focused regression
tests, and run narrow checks when useful. AWF and GitHub CI own broad validation,
coverage, build, and merge-gating provenance.

## Scope

- Stop the currently running full-coverage subprocesses inside the affected
  workspaces without killing the whole workspace.
- Add a global agent prompt boundary to the adapter preamble so all providers get
  the rule, not just one task prompt.
- Keep the rule generic: focused tests are allowed; broad suites and coverage
  gates are AWF/GitHub-managed.
- Add regression coverage for the prompt boundary.

## Requirements Checklist

- Agents are explicitly told not to run full `.awf/workspace.yml` validation,
  full coverage gates, whole-repo test suites, or full frontend builds inside
  the agent phase.
- Agents are told to run focused tests/lint only when useful for the files or
  behavior they changed.
- Agents are told validation docs may describe AWF/GitHub-managed validation
  without executing it themselves.
- The instruction is injected by AWF for every adapter run.
- Current runaway coverage subprocesses are stopped.
- Unit tests prove the preamble contains the validation boundary.

## Implementation Steps

1. Kill only the nested full-coverage subprocess groups in the two active
   salvage workspaces, leaving Claude/workspaces alive.
2. Update `_AWF_PROMPT_PREAMBLE` in `src/awf/adapters/base.py`.
3. Add/adjust adapter tests to assert the validation boundary is present in the
   stdin prompt.
4. Run focused adapter tests and lint/type checks for touched files.
5. Rebuild/restart the local AWF API/worker so future runs use the boundary.
6. Monitor the two active workspaces for re-running broad validation; cancel and
   retry with the new boundary if they do.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py -q
uv run --python 3.12 --extra dev ruff check src/awf/adapters/base.py tests/unit/adapters/test_adapters.py
uv run --python 3.12 --extra dev mypy src/awf/adapters/base.py
```
