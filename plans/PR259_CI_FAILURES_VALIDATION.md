# PR259 CI Failures Validation

Plan reference: `plans/PR259_CI_FAILURES_PLAN.md`

## Requirement Status

- Preserve AWF workspace contract: Complete. Work stayed on the current branch; no push, rebase, force-push, or branch switch was performed.
- Reproduce the focused failures first: Complete. The provided focused pytest command failed before implementation with the refresh operation id mismatch and reason catalog failures.
- Fix refresh endpoint coalescing: Complete. `WorkspaceControlService.request_refresh_workspace` now leaves operator refresh operations pending, allowing same-payload fresh-key requests to coalesce to the active operation.
- Fix reason catalog drift: Complete. Public artifact/callback/MCP/PR adoption and existing catalog reason codes are represented in `_REASON_TEXT`; `docs/REASON_CATALOG.md` was regenerated from `scripts/generate_reason_catalog.py`.
- Run focused repro until green: Complete. The exact CI repro command passes.
- Run narrow adjacent validation: Complete. Refresh lifecycle/MCP contract tests, ruff, mypy, and whitespace checks pass.
- Record validation: Complete. This file documents implementation evidence and command results.
- Commit locally: Complete. This validation record is included with the local CI-fix commit.

## Evidence

Files changed:

- `src/awf/service/controls.py`
- `src/awf/service/doctor/reasons.py`
- `docs/REASON_CATALOG.md`
- `tests/unit/service/test_controls_lifecycle.py`
- `tests/unit/mcp/test_mcp_control_contracts.py`
- `plans/PR259_CI_FAILURES_PLAN.md`
- `plans/PR259_CI_FAILURES_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency.py::test_refresh_endpoint_returns_operation_response_and_coalesces_active_request tests/unit/service/test_doctor_reasons.py::test_reason_catalog_is_synchronized_with_python_source tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage -q`
  - Before fix: failed with refresh operation id mismatch and reason catalog coverage/sync failures.
  - After fix: passed, `3 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle.py::test_refresh_active_workspace_keeps_operation_pending_and_coalesces_new_same_payload_request tests/unit/service/test_controls_lifecycle.py::test_refresh_fresh_key_with_stale_if_match_does_not_coalesce_active_operation tests/unit/service/test_controls_lifecycle.py::test_refresh_replays_same_idempotency_key_after_destroying_state tests/unit/mcp/test_mcp_control_contracts.py::TestRealDbPaths::test_refresh_creates_operation_row -q`
  - Before implementation after test update: failed as expected while refresh was still auto-finished.
  - After fix: passed, `4 passed`.
- `uv run python scripts/generate_reason_catalog.py`
  - Passed and regenerated `docs/REASON_CATALOG.md`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py src/awf/service/doctor/reasons.py tests/unit/service/test_controls_lifecycle.py tests/unit/mcp/test_mcp_control_contracts.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/controls.py src/awf/service/doctor/reasons.py`
  - Passed.
- `git diff --check`
  - Passed.

## Gaps

None. The reported CI failures are fixed by the focused and adjacent validation listed above.
