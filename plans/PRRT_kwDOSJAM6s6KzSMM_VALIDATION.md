# PRRT_kwDOSJAM6s6KzSMM Validation

## Plan Alignment

- Verified the review against local code: `_run_pre_push_validation` used
  `operation_start_head or workspace_head_sha` after `verify_head_object_exists`
  reported the current HEAD object missing.
- Added a regression test proving the no-operation-anchor path uses the open
  merge-candidate PR head instead of the broken `rev-parse HEAD` value.
- Updated `_run_pre_push_validation` to match the existing
  `_commit_dirty_worktree` missing-HEAD fallback contract.
- Updated neighboring recovered-HEAD tests to pass `operation_start_head`
  explicitly where they are exercising operation-anchored recovery behavior.

## Focused Checks

- Red step: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py::test_pre_push_validation_missing_head_uses_candidate_recovery_anchor -q`
  failed before the code change because `workspace_head_sha` was the broken
  `rev-parse HEAD` SHA.
- Green step: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py::test_pre_push_validation_missing_head_uses_candidate_recovery_anchor -q`
  passed after the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q`
  passed: 10 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after agent completion.
