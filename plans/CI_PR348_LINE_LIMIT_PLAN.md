# CI PR 348 Line Limit Plan

## Problem Statement and Scope

PR #348 fails the maintainability guardrail because
`src/awf/control/executor/monitor_handoff.py` has 1601 lines, exceeding the
1500-line first-party file limit. The fix must preserve monitor handoff behavior
while decomposing the oversized module.

## Requirements Checklist

- Keep all first-party code files at or below the 1500-line limit.
- Preserve existing monitor handoff behavior and private helper compatibility
  used by executor mixins/tests.
- Do not weaken, skip, or disable the maintainability check.
- Avoid protected workflow or quality-gate configuration changes.
- Use focused local validation only; AWF/GitHub owns broad CI and coverage.
- Commit the fix locally on the current AWF-managed branch.

## Implementation Steps

1. Reproduce the reported maintainability failure with the provided pytest node.
2. Extract self-contained PR audit/setup-dependency event helpers from
   `monitor_handoff.py` into a focused executor helper module.
3. Re-export/import the extracted helpers through `monitor_handoff.py` so
   existing delegate wiring remains stable.
4. Remove imports that are only needed by the extracted helper module.
5. Run the focused maintainability repro and narrow tests covering the moved
   helper behavior.
6. Record validation evidence in `plans/CI_PR348_LINE_LIMIT_VALIDATION.md`.
7. Commit the scoped fix locally with a conventional commit message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes and reports no oversized first-party files.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_002.py::TestHappyPathPart001::test_drives_ready_to_completed_and_records_pr_url -q`
  - Passes for the extracted audit helper path.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_005.py::TestExecutorCoverageEdgesPart001::test_executor_setup_dependency_retry_success_preserves_lineage_and_runs_agent tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_005.py::TestExecutorCoverageEdgesPart001::test_executor_setup_dependency_retry_exhausted_marks_precise_setup_failure -q`
  - Passes for the extracted setup-dependency event helper path.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py src/awf/control/executor/monitor_handoff_audit.py`
  - Passes with no lint errors in the touched modules.
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/monitor_handoff.py src/awf/control/executor/monitor_handoff_audit.py`
  - Passes with no type errors in the touched modules.
