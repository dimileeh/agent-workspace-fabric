# CI PR328 Line-Limit Fix Validation

Plan reference: `plans/CI_PR328_LINE_LIMIT_PLAN.md`

## Requirement Status

- Keep the maintainability check unchanged: Complete.
- Bring each reported oversized first-party file under 1,500 lines: Complete.
  - `tests/unit/control/test_worker_parts/test_worker_part_003.py`: 1,478 lines.
  - `tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py`: 1,250 lines.
  - `tests/unit/service/test_workspace_retry.py`: 1,480 lines.
- Preserve existing test behavior by moving cohesive test groups/helpers:
  Complete.
  - Added `tests/unit/control/test_worker_parts/test_worker_part_043.py`.
  - Added `tests/unit/node/test_provisioner_parts/test_provisioner_part_006.py`.
  - Added `tests/unit/service/test_workspace_retry_sync_branch.py`.
- Avoid broad AWF/GitHub-owned validation: Complete.
- Commit the local fix with a conventional commit message: Complete.

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_043.py tests/unit/node/test_provisioner_parts/test_provisioner_part_006.py tests/unit/service/test_workspace_retry_sync_branch.py -q`
  - Result: passed, 9 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py::TestOperatorControlRaces::test_orphan_stop_timeout_records_false_in_payload tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Result: passed, 2 tests.
- `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_worker_parts/test_worker_part_003.py tests/unit/control/test_worker_parts/test_worker_part_043.py tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py tests/unit/node/test_provisioner_parts/test_provisioner_part_006.py tests/unit/service/test_workspace_retry.py tests/unit/service/test_workspace_retry_sync_branch.py`
  - Result: passed.
- `git diff --check`
  - Result: passed.

Full AWF/GitHub validation, full coverage gates, and CI-required aggregate
checks were not run locally because AWF owns broad validation after agent
completion.
