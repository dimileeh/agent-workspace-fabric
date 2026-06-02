# CI PR328 Line-Limit Fix Plan

## Problem Statement And Scope

PR #328 fails CI because `test_first_party_code_files_stay_under_line_limit`
reports three first-party test files over the 1,500-line maintainability cap.
The provided focused repro shows the operator-control race test now passes, so
this fix is scoped to restoring the file-size guardrail without weakening or
skipping the check.

## Requirements Checklist

- Keep the maintainability check unchanged.
- Bring each reported oversized first-party file under 1,500 lines:
  - `tests/unit/control/test_worker_parts/test_worker_part_003.py`
  - `tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py`
  - `tests/unit/service/test_workspace_retry.py`
- Preserve existing test behavior by moving cohesive test groups and any needed
  helpers into appropriately named test modules.
- Avoid broad AWF/GitHub-owned validation; run focused local checks only.
- Commit the local fix with a conventional commit message.

## Implementation Steps

1. Inspect the oversized files and identify cohesive classes or test groups that
   can move cleanly into new modules.
2. Move only enough tests/helpers to bring each offender under the line cap.
3. Run targeted checks for the moved tests plus the original focused repro.
4. Create a validation document recording requirement status and evidence.
5. Commit the scoped changes locally.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest <moved/original targeted node ids> -q`
  passes for moved behavior.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_timeout_records_false_in_payload tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passes for the CI-provided focused repro.
- Full AWF/GitHub validation is not run locally; AWF owns broad validation after
  agent completion.
