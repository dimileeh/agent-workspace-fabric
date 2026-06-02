# Comment 4585090228 Cancelled Runtime Guard Plan

## Problem Statement And Scope

Review-level comment `issue:4585090228` raised three concerns in the
host-port release and planning auto-retry lifecycle. The cleanup resume
transaction concern and same-timestamp cleanup scan concern are already
addressed in the current branch by durable resume-failed events, a separate
post-release resume path, a bounded recovery scan, and deterministic release
event ordering.

The remaining actionable gap is the cancelled source-runtime shortcut in
`_source_runtime_not_yet_released`: a cancelled source with host ports, no
compose metadata, no node, no reservation history, and no release event is
currently treated as having no runtime. That is safe for a workspace cancelled
before provisioning placement, but not for legacy rows that reached
`provisioning` before cancellation and lack durable pre-launch or release
evidence.

Scope is limited to the retry source-runtime guard and focused regression
coverage.

## Requirements Checklist

- Preserve the existing safe retry path for rows cancelled before provisioning
  placement (`requested -> cancelled`) with no runtime evidence.
- Reject cancelled rows that reached provisioning and have host ports but lack
  terminal runtime release or explicit pre-launch failure evidence.
- Preserve failed/null-runtime and pre-launch failure behavior already covered
  by retry-port tests.
- Document cleanup review concerns as already addressed by current branch code
  and tests, without changing cleanup behavior in this pass.
- Run targeted local checks only; full AWF/GitHub validation remains owned by
  AWF after agent completion.

## Implementation Steps

1. Add a focused regression test for a cancelled source that transitioned from
   `provisioning`, has host ports, no node, no compose metadata, no reservation
   history, and no terminal release/pre-launch evidence.
2. Confirm the regression fails before the production change when practical.
3. Add a small helper that reads the latest `workspace.state_changed` event into
   `cancelled` and recognizes only `requested -> cancelled` as the no-runtime
   shortcut.
4. Update `_source_runtime_not_yet_released` so cancelled no-runtime rows that
   were not cancelled before provisioning fall through to the existing
   pre-launch-failure evidence gate.
5. Re-run the new regression and nearby retry-port tests that preserve the
   existing intended cases.
6. Record plan validation evidence in
   `plans/COMMENT_4585090228_CANCELLED_RUNTIME_GUARD_VALIDATION.md`.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_rejects_cancelled_provisioning_null_runtime_source_without_reservation -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_allows_early_cancelled_source_without_runtime_evidence tests/unit/service/test_workspace_retry_port.py::test_retry_allows_when_source_compose_project_name_is_none tests/unit/service/test_workspace_retry_port.py::test_retry_rejects_legacy_null_runtime_source_without_reservation tests/unit/service/test_workspace_retry_port.py::test_retry_rejects_node_stamped_legacy_null_runtime_source_without_reservation tests/unit/service/test_workspace_retry_port.py::test_retry_rejects_cancelled_provisioning_null_runtime_source_without_reservation -q
uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces_retry.py tests/unit/service/test_workspace_retry_port.py
```

All focused commands must pass. Do not run full coverage, whole-repository unit
suites, frontend builds, OpenAPI drift checks, or CI-equivalent validation in
this workspace phase.
