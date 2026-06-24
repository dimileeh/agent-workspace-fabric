# PR #673 CI Coverage Fix Plan

## Problem Statement and Scope

PR #673 fails CI only in `python-full-coverage`. The coverage combine step reports
combined line+branch coverage of `98.996%`, below the required `99.00%`.

Scope is limited to owned runtime monitor paths and tests. Do not edit workflow,
coverage threshold, or unowned protected files. Do not run broad AWF/GitHub-owned
validation locally.

## Requirements Checklist

- Identify the coverage miss from CI logs before changing code.
- Add a meaningful focused test for reachable behavior in an owned changed area.
- Keep changes minimal and avoid weakening or skipping checks.
- Run targeted validation only for the changed tests/module.
- Record validation evidence and note that broad validation is handled by AWF/CI.

## Implementation Steps

1. Inspect CI coverage logs and choose a reachable uncovered branch in
   `src/awf/runtime/pr_monitor_runner`.
2. Add a focused regression test that asserts the selected branch's behavior.
3. Run the targeted test file or test case.
4. Run focused lint for changed files if practical.
5. Create `plans/PR673_CI_COVERAGE_FIX_VALIDATION.md`.
6. Commit the scoped fix locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest <targeted test> -q`
  - Passes.
- `uv run --python 3.12 --extra dev ruff check <changed files>`
  - Passes.

Full coverage and full CI validation are intentionally not run locally; AWF and
GitHub CI own broad validation after this agent phase.
