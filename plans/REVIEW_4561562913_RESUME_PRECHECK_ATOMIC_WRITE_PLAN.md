# Review 4561562913 Resume Precheck Atomic Write Plan

## Problem Statement and Scope

PR review comment `issue:4561562913` flags two monitor resume risks in
`src/awf/control/executor/monitor_handoff.py`:

- required companion env-secret precheck reports only the first unavailable
  required source;
- optional companion env-secret refresh rewrites the persisted Compose file with
  a direct `write_text`, so a mid-write crash can corrupt the file.

Scope is limited to the monitor resume helper behavior and focused regression
tests for those helpers.

## Requirements Checklist

- [x] Required env-secret precheck reports all unavailable required env sources
      in one `CompanionEnvSecretPrecheckError`.
- [x] Existing single-failure reason-code behavior remains compatible for
      missing and empty required sources.
- [x] Optional env-secret refresh writes through a sibling temporary file and
      atomically replaces the persisted Compose file.
- [x] Refresh still writes only Compose interpolation placeholders and never raw
      secret values.
- [x] Focused tests cover the changed behavior.
- [x] Broad AWF/GitHub validation is not run during this agent phase.

## Implementation Steps

1. Add failing unit tests for aggregated precheck diagnostics and atomic refresh
   write behavior.
2. Update `_precheck_required_companion_env_secrets_for_resume` to collect all
   required env-source failures before raising.
3. Add an atomic text-write helper for compose refresh and replace the direct
   `compose_file.write_text` call.
4. Run only targeted tests for the changed monitor handoff behavior.
5. Record validation evidence in
   `plans/REVIEW_4561562913_RESUME_PRECHECK_ATOMIC_WRITE_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py::test_required_companion_env_secret_precheck_reports_all_unavailable_sources tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py::test_companion_env_secret_refresh_avoids_direct_target_file_write -q`
  - Passes after implementation and fails before implementation when practical.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_010.py::TestExecutorCoverageEdgesPart010::test_resume_pr_monitor_stops_after_required_companion_env_secret_precheck_failure -q`
  - Existing single-failure resume behavior remains compatible.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
