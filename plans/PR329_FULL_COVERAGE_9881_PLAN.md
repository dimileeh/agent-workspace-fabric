# PR329 Full Coverage 98.81 Plan

## Problem Statement And Scope

PR #329's GitHub Actions `python-full-coverage` job passed all tests but failed
the 99% coverage gate at 98.81%. The CI coverage report shows the largest
branch-local gap in newly split PR monitor path helper code, especially
`src/awf/runtime/pr_monitor_runner/path_helpers.py`, with smaller live edge
branches in operator hint state helpers.

Scope is limited to restoring the full-coverage gate without changing workflow
configuration or weakening any check.

## Requirements Checklist

- Do not edit protected workflow, quality-gate, or repository configuration files.
- Keep the fix on the current AWF-managed branch and do not push or rebase.
- Remove dead duplicate helper code only when the repo already routes behavior
  through the canonical helper module.
- Add focused tests for remaining live helper behavior where tests improve the
  coverage gap.
- Run only focused local validation; broad AWF/GitHub validation remains owned
  by AWF after agent completion.
- Commit the fix locally with a conventional `fix(ci): ...` message.

## Implementation Steps

1. Inspect CI coverage output and changed helper modules to identify the
   smallest coverage gap.
2. Remove unused duplicate path parsing helpers from `path_helpers.py`; the
   canonical parsing implementation remains in `path_parsing.py`.
3. Add focused unit tests for live `path_helpers.py` and `operator_hints.py`
   branches that CI marked uncovered.
4. Run targeted pytest for the edited tests and focused lint/type checks for
   touched Python files.
5. Save validation evidence in `plans/PR329_FULL_COVERAGE_9881_VALIDATION.md`
   and commit locally.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest <focused test nodes> -q`
  - Passes all focused tests covering edited helper behavior.
- `uv run --python 3.12 --extra dev ruff check <touched files>`
  - Passes for the edited source/test files.

Full repository coverage and CI-required aggregation are intentionally not run
inside the agent phase; AWF/GitHub own those broad gates after completion.
