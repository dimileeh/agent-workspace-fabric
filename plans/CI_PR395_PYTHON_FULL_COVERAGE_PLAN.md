# CI PR395 Python Full Coverage Plan

## Problem Statement And Scope

PR #395 fails the Python full coverage job on focused unit tests. Local focused repro confirmed failures in `/readyz` heartbeat/readiness behavior, `ControlWorker.run_once()` heartbeat side effects, an environment-dependent egress-audit readiness test, and the first-party line-limit guard for `tests/unit/api/test_health_parts/test_health_part_001.py`.

Scope is limited to the failing Python unit behavior and required plan/validation artifacts. Do not change workflow/configuration gates, run broad coverage locally, push, or switch branches.

## Requirements Checklist

- `/readyz` must return structured 503 readiness output when worker heartbeat lookup cannot run, not an unhandled exception.
- `ControlWorker.run_once()` heartbeat/prune maintenance must not crash tests or workers when the maintenance path encounters a non-callable/miswired session factory; the core run-once dispatch path remains testable.
- Commit-boundary worker tests must assert ordered-decision behavior without counting unrelated heartbeat/prune commits.
- Readiness tests focused on Docker/orphan-resource or egress-audit behavior must isolate unrelated worker/provider readiness dependencies.
- `tests/unit/api/test_health_parts/test_health_part_001.py` must be reduced below the 1500-line maintainability guard without deleting behavior coverage.
- Verification must use focused repro commands only; full AWF/GitHub validation and coverage remain managed by AWF after agent completion.

## Implementation Steps

1. Update heartbeat readiness/maintenance exception handling narrowly so side checks report/log failures instead of escaping on fake/miswired factories.
2. Patch focused unit fixtures/tests to isolate worker heartbeat and provider readiness where those dependencies are not under test.
3. Move a small cohesive group of health tests from `test_health_part_001.py` into a new `test_health_part_003.py`, reusing local helpers.
4. Re-run the AWF-provided focused repro and the remaining failing node IDs.
5. Create `plans/CI_PR395_PYTHON_FULL_COVERAGE_VALIDATION.md` with requirement status and focused command evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_001.py::test_readyz_db_query_failure_returns_503 tests/unit/api/test_health_parts/test_health_part_001.py::test_readyz_db_closed_connection_returns_specific_diagnostic tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_terminal_workspace_with_only_retained_worktree_stays_healthy tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_retains_recent_terminal_worktree_without_failing tests/unit/control/test_worker_parts/test_worker_part_001.py::TestRunOncePart001::test_stale_requested_candidates_are_filtered_before_provision_slot_truncation -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/control/test_worker_parts/test_worker_part_007.py::TestRunOncePart007::test_requested_ordered_decision_persistent_transient_commit_failure_prevents_dispatch tests/unit/control/test_worker_parts/test_worker_part_007.py::TestRunOncePart007::test_requested_ordered_decision_ambiguous_commit_retries_without_duplicate tests/unit/control/test_worker_parts/test_worker_part_046.py::test_run_once_invokes_classified_orphan_reaper_loop tests/unit/api/test_egress_audit.py::test_readyz_includes_egress_audit_check -q`

Pass criteria: all listed focused tests pass locally. Full coverage/CI is intentionally not run in this agent phase.
