# Protected Quality Gate Review 4491715538 Plan

## Problem Statement And Scope

Address PR review comment `issue:4491715538` for protected quality-gate diff handling.
The scope is limited to:

- Make coverage threshold edits in `pyproject.toml` report a specific violation
  reason when `tool.coverage.report.fail_under` is raised or unchanged as part
  of a `tool.coverage` policy section change.
- Extract duplicated committed protected-file diff Git helpers shared by the
  executor and PR monitor runner.

## Requirements Checklist

- Add or update regression tests before implementation.
- Preserve fail-closed behavior for protected file classification.
- Preserve existing safe deletion/new-file behavior for `git show` missing paths.
- Avoid branch switches and pushes; commit locally on the existing AWF branch.
- Keep changes narrow and avoid weakening existing quality-gate tests.

## Implementation Steps

1. Add a quality-gate regression test for raised coverage `fail_under` with a
   specific reason and section.
2. Add shared helper coverage for committed protected-file diffs so both runtime
   call sites can depend on one Git error heuristic.
3. Implement a shared helper module for `git show` text loading and committed
   protected-file diffs.
4. Update executor and PR monitor runner to use the shared helpers.
5. Update the coverage policy section classifier to emit a specific reason for
   non-lowering numeric `fail_under` edits.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py tests/unit/control/test_protected_file_diffs.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py src/awf/control/protected_file_diffs.py src/awf/control/executor.py src/awf/runtime/pr_monitor_runner.py tests/unit/control/test_quality_gates.py tests/unit/control/test_protected_file_diffs.py`
  must pass.
- `uv run --python 3.12 --extra dev mypy src/awf`
  must pass or any unrelated pre-existing failure must be documented.
