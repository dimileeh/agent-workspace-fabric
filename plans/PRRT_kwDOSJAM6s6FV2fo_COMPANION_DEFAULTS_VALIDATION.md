# PRRT_kwDOSJAM6s6FV2fo Companion Defaults Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6FV2fo_COMPANION_DEFAULTS_PLAN.md`

## Requirement Status

- Complete: Confirm the feedback is actionable from the local code.
  - Evidence: The new regression failed before the implementation with
    `WorkspaceCreateIdempotencyConflictError`.
- Complete: Preserve existing requested companion task-policy snapshots.
  - Evidence: `_requested_companions()` remains unchanged.
- Complete: Normalize stored companion entries so missing default
  `environment_secrets` compares as `{}` on identical create replays.
  - Evidence: `_stored_companions()` now normalizes valid stored companion
    mappings through `WorkspaceCompanionRequest`.
- Complete: Do not weaken mismatches for genuinely different companion
  requests.
  - Evidence: Invalid stored companion mappings are preserved as raw dicts on
    validation failure, and the existing companion mismatch regression still
    passes.
- Complete: Run only focused validation.
  - Evidence: Focused commands below were run; full AWF/GitHub validation is
    intentionally left to AWF after agent completion.

## Focused Validation Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_idempotency.py::test_create_replays_legacy_companion_without_environment_secrets -q`
  - First run before implementation: failed with
    `WorkspaceCreateIdempotencyConflictError`.
  - Final run after implementation: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces_direct.py::TestCreateDirect::test_create_persists_companions_and_uses_them_for_idempotency -q`
  - Passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces_create.py tests/unit/service/test_workspace_idempotency.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/workspaces_create.py`
  - Passed.

## Gaps

No planned gaps remain. Broad validation, coverage gates, OpenAPI drift checks,
and CI-equivalent suites are managed by AWF/GitHub after agent completion.
