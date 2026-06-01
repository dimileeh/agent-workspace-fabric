# Review 4587587225 Audit Source Head Plan

## Problem Statement And Scope

Address the review concern that monitor handoff audit events can silently replace
an intentionally missing `source_head_sha` with `workspace.monitor_last_commit_sha`.
The scope is limited to the audit helper contract and focused regression coverage.

## Requirements Checklist

- Preserve the existing documented fallback when callers omit `source_head_sha`.
- Preserve an explicit `source_head_sha=None` as `null` in the audit payload.
- Keep the change scoped to the audit helper, focused unit tests, and required
  plan/validation artifacts.
- Run only targeted checks for the changed behavior; AWF/GitHub owns broad
  validation after agent completion.

## Implementation Steps

1. Add a focused failing regression proving explicit `source_head_sha=None` is
   persisted as `None` even when `workspace.monitor_last_commit_sha` is set.
2. Update the audit helper to distinguish omitted `source_head_sha` from an
   explicit `None`.
3. Run the targeted audit helper tests and a focused lint check for touched
   Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_005.py::test_executor_pr_audit_event_preserves_explicit_none_source_head_sha -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_005.py::test_executor_pr_audit_event_defaults_source_head_sha_from_workspace tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_005.py::test_executor_pr_audit_event_preserves_explicit_none_source_head_sha -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff_audit.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_005.py`
- Pass criteria: targeted tests and focused lint pass; no broad AWF/GitHub-owned
  validation suite is run locally.
