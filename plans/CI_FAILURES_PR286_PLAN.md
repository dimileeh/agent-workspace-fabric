# PR 286 CI Failure Fix Plan

## Problem Statement

PR #286 fails the Python CI surface in focused unit tests. The local focused
repro confirms failures in health-check event payload handling, maintainability
line-limit guardrails, workspace-create CLI helper enum coercion, legacy memory
warning monkeypatchability, and a stale fix-pass warning expectation that
conflicts with the safer validation-cycle invariant for failed git operations.

## Scope

- Fix only the focused Python unit-test failures reported by CI.
- Preserve AWF's validation provenance and reason-code behavior.
- Do not weaken, skip, or remove CI checks.
- Do not run broad AWF/GitHub-owned validation locally; AWF owns that after
  agent completion.

## Requirements Checklist

- Health-check failure event payloads expose empty `stream_ids` when command
  metadata is missing or not a mapping.
- `awf workspace create` builds the same v1 payload when tests call the Typer
  command helper with raw enum strings instead of enum instances.
- Legacy numeric memory values without units still warn through the
  monkeypatchable `awf.service.workspaces_create._log` logger.
- First-party Python test files stay under the 1,500-line maintainability
  limit without deleting coverage.
- Validation fix-pass git add/diff/commit failures continue to fail the
  workspace with explicit reason codes, and stale tests are aligned with that
  invariant.
- Focused repro tests pass locally.

## Implementation Steps

1. Add small normalization helpers for CLI enum/string inputs and update
   workspace-create request construction.
2. Make the workspace-create module's logger local and monkeypatchable, then
   use it for missing memory-unit warnings.
3. Align the health-check regression with the existing event payload contract
   and keep empty `stream_ids` serialized in the event payload.
4. Split oversized executor test-part files by moving trailing independent
   tests into new part files under the existing test directories.
5. Update the stale final-polish fix-pass warning test to assert the existing
   reason-coded infrastructure failure and warning log behavior.

## Verification

Run only focused commands:

- Provided CI repro:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_005.py::test_healthcheck_failure_event_handles_none_metadata tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/service/test_scheduler_records.py::test_legacy_numeric_memory_without_unit_warns tests/unit/cli/test_workspace_commands_helpers.py::test_workspace_create_builds_full_v1_payload tests/unit/cli/test_workspace_commands_helpers.py::test_workspace_create_builds_minimal_development_payload -q`
- Additional failing CI node:
  `uv run --python 3.12 --extra dev pytest tests/unit/test_final_polish.py::TestExecutorFixPassWarnings::test_fix_pass_add_and_commit_failures_log_and_continue -q`
- Conflict guard:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_validation_fix_cycle.py::TestFixPassGitCommandFailures::test_fix_pass_git_failure_fails_workspace_and_validate_operation -q`

Pass criteria: all focused commands pass. Full coverage and broad CI-equivalent
validation remain AWF/GitHub responsibilities after this agent phase.
