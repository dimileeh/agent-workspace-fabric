# Review 4578892384 Profile Sync Plan

## Problem Statement And Scope

PR review comment `issue:4578892384` identifies duplicated executor logic that persists a
resolved profile snapshot and realigns the in-memory profile across
`execution_flow.py` and `monitor_handoff.py`. It also asks for an explicit comment on
the conditional side effect in `_profile_for_workspace`.

Scope is limited to the executor profile snapshot sync path and focused regression
coverage for the extracted helper. No AWF/GitHub-owned broad validation will be run
inside this agent phase.

## Requirements Checklist

- Add a shared helper for "persist snapshot, then realign the in-memory profile".
- Replace the duplicated call-site blocks in `execution_flow.py` and
  `monitor_handoff.py` with the shared helper.
- Preserve the existing first-writer snapshot semantics and planning iteration default
  handling.
- Document `_profile_for_workspace`'s conditional mutation when resolving from scratch.
- Add focused regression coverage for the shared helper.
- Run only focused validation for the touched behavior and files.

## Implementation Steps

1. Add a failing unit test for the new shared helper in
   `tests/unit/control/test_executor_runtime_profile_snapshot.py`.
2. Implement `_sync_resolved_profile` in `src/awf/control/executor/state_ops.py`.
3. Update executor call sites to use `_sync_resolved_profile`.
4. Add the requested side-effect comment in `src/awf/control/executor/helpers.py`.
5. Run the focused unit test, then focused ruff on touched Python files.
6. Create the validation document with requirement-by-requirement evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py -q`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/state_ops.py src/awf/control/executor/execution_flow.py src/awf/control/executor/monitor_handoff.py src/awf/control/executor/helpers.py tests/unit/control/test_executor_runtime_profile_snapshot.py`
  - Passes with no lint errors.

Full AWF/GitHub validation, broad test suites, coverage gates, and CI-equivalent checks
are managed by AWF after agent completion.

## Iteration 2: Validation Call-Site And Non-Dict RETURNING Gap

### Problem Statement And Scope

The follow-up review on `issue:4578892384` identifies two remaining gaps in the
runtime profile snapshot path:

- `execution_validation.py` still calls `_profile_for_workspace` without pairing it
  with `_sync_resolved_profile`, so validation can use and later persist a freshly
  recomputed auto profile instead of the first frozen snapshot.
- `_persist_resolved_profile_snapshot_if_missing` commits only when `RETURNING`
  yields a `dict`; a driver that returns the JSON column as a string can roll back a
  successful first-writer update.

Scope is limited to those two executor profile-snapshot behaviors and focused
regression coverage.

### Requirements Checklist

- Pair the validation-phase `_profile_for_workspace` call with `_sync_resolved_profile`
  and use the synced profile for command planning and tier selection.
- Preserve first-write-wins behavior when another executor already froze a snapshot.
- Commit a successful atomic snapshot update even when `RETURNING` yields a JSON string.
- Normalize returned or selected JSON-string snapshots to dictionaries when possible.
- Add focused regression coverage for both gaps using strict test-first workflow.
- Run only focused tests/lint for touched files; AWF/GitHub own broad validation after
  agent completion.

### Implementation Steps

1. Add failing regression tests in `tests/unit/control/test_executor_runtime_profile_snapshot.py`
   for validation sync usage and JSON-string `RETURNING` commit behavior.
2. Confirm the focused tests fail before implementation.
3. Import and call `_sync_resolved_profile` in
   `src/awf/control/executor/execution_validation.py` immediately after
   `_profile_for_workspace`.
4. Harden `_persist_resolved_profile_snapshot_if_missing` in
   `src/awf/control/executor/state_ops.py` so non-`None` `RETURNING` values are treated
   as successful updates, committed, and normalized.
5. Re-run the focused regression tests and focused ruff on touched Python files.
6. Update the validation document with requirement-by-requirement evidence.

### Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_runtime_profile_snapshot.py -q`
  - New tests fail before implementation and pass after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/state_ops.py src/awf/control/executor/execution_validation.py tests/unit/control/test_executor_runtime_profile_snapshot.py`
  - Passes with no lint errors.

Full AWF/GitHub validation, broad test suites, full coverage gates, and CI-equivalent
commands are managed by AWF after agent completion.
