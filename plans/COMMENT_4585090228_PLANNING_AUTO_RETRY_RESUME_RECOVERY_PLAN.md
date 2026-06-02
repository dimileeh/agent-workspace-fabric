# Comment 4585090228 Planning Auto-Retry Resume Recovery Plan

## Problem Statement and Scope

Review-level comment `issue:4585090228` reports that cleanup records
`workspace.terminal_runtime_released` before calling the planning-scope
auto-retry resume hook. If that hook raises after the release event commits,
the workspace becomes effectively released and is excluded from later terminal
runtime cleanup scans, leaving the blocked auto-retry stranded.

Scope is limited to durable recovery for this terminal-release resume path.
Do not change host-port admission semantics, retry admission checks, or broad
cleanup ownership.

## Requirements Checklist

- Preserve `workspace.terminal_runtime_released` as the authoritative runtime
  release marker after cleanup succeeds.
- Add durable audit evidence when the planning-scope auto-retry resume hook
  fails after terminal runtime release.
- Make later worker scans retry unresolved planning-scope auto-retries whose
  source runtime is already effectively released.
- Avoid re-running Docker/runtime cleanup just to resume a blocked auto-retry.
- Keep existing terminal planning auto-retry terminal events (`requested`,
  `skipped`, `failed`, and manual retry request) as blockers for further
  resume attempts.
- Run targeted local validation only; full AWF/GitHub validation remains owned
  by AWF after agent completion.

## Implementation Steps

1. Add regression coverage proving a resume-failed marker remains a pending
   terminal-release planning auto-retry, and that a later worker scan calls the
   resume hook for a released workspace without invoking runtime cleanup.
2. Run the focused failing regressions before production changes where
   practical.
3. Add a dedicated `workspace.planning_scope_auto_retry_resume_failed` event
   and reason code in the planning auto-retry helpers.
4. Record that event best-effort when the cleanup-worker post-release resume
   hook raises.
5. Add a bounded cleanup-worker recovery scan for effectively released
   terminal workspaces whose latest planning auto-retry event still represents
   an unresolved terminal-release block.
6. Re-run focused regression tests and touched-file lint/type checks.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_planning_scope_auto_retry_pending_check_treats_resume_failed_as_unresolved tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_planning_scope_auto_retry_resume_failure_records_recoverable_event tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_resumes_pending_planning_scope_auto_retry_after_recorded_release -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_terminal_runtime_release_ignores_blocked_planning_scope_resume_failure tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_terminal_runtime_release_event_triggers_blocked_planning_scope_resume tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_resumes_pending_planning_scope_auto_retry_after_recorded_release -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py::test_release_terminal_runtime_resources_propagates_single_candidate_error tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py::test_release_terminal_runtime_resources_groups_multiple_candidate_errors -q
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_ops.py src/awf/control/worker/cleanup.py src/awf/control/worker/mixins.py tests/unit/control/test_executor_planning_auto_retry_transactions.py tests/unit/control/test_worker_parts/test_worker_part_042.py tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py
uv run --python 3.12 --extra dev mypy src/awf/control/executor/planning_ops.py src/awf/control/worker/cleanup.py src/awf/control/worker/mixins.py
```

All focused commands must pass. Do not run full coverage, whole-repository unit
suites, frontend builds, OpenAPI drift checks, or CI-equivalent validation in
this workspace phase.
