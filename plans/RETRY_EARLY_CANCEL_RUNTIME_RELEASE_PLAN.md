# Retry Early-Cancel Runtime Release Plan

## Problem Statement And Scope

PR review comment `issue:4585090228` reports that retrying a workspace cancelled
before scheduler/provisioner claim can be blocked by `SOURCE_RUNTIME_NOT_RELEASED`
until the cleanup sweep emits `workspace.terminal_runtime_released`. Such a
workspace has no compose metadata, no node placement, and no resource reservation,
so it has no known runtime that could still hold host ports.

Scope is limited to the retry runtime-release predicate and focused regression
coverage. Existing safety coverage for unreleased failed or legacy null-runtime
sources must remain intact.

## Requirements Checklist

- Allow retry for a cancelled source with host ports when all runtime placement
  evidence is absent: `compose_project_name`, `compose_file_path`, `node_id`, and
  `ResourceReservation`.
- Preserve the existing block for failed legacy null-runtime sources without
  release evidence.
- Preserve the existing block for real unreleased runtime sources with compose
  metadata, node placement, or reservation evidence.
- Treat the provisioner composite-lock note as verification-only unless current
  code or tests contradict the accepted first-committer-wins behavior.
- Do not run broad AWF/GitHub-owned validation; use focused tests only.

## Implementation Steps

1. Add a focused regression test in `tests/unit/service/test_workspace_retry_port.py`
   for the early-cancelled, no-runtime-evidence case.
2. Confirm that test fails against the current predicate.
3. Update `_source_runtime_not_yet_released` in
   `src/awf/service/workspaces_retry.py` to return released for cancelled rows
   with no runtime metadata and no reservations.
4. Re-run the new test and adjacent retry-port tests that guard source runtime
   blocking.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py -k "early_cancelled or legacy_null_runtime_source_without_reservation or host_port_conflict_with_source" -q`

Pass criteria: the new early-cancelled retry case passes, and existing source
runtime safety cases continue to pass. Full AWF/GitHub validation remains owned
by AWF after this agent phase.
