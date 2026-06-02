## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6GX2bd` reports that retry admission treats a
terminal failed source workspace as safe when `compose_project_name` and
`compose_file_path` are `NULL` but `node_id` is stamped. Legacy provisioner
failures can leave leaked `awf_<workspace_id>` containers in exactly that state,
so retrying before terminal cleanup records an effective release can admit a new
workspace that later fails at Compose host-port binding.

Scope is limited to the retry source-runtime release guard and focused service
tests for host-port retry behavior.

## Requirements Checklist

- Add a regression test for a failed source with host ports, `node_id` stamped,
  null compose metadata, no reservation history, and no terminal runtime release
  event.
- Keep early cancelled rows with no runtime evidence retryable.
- Keep modern pre-launch failed rows with reservation evidence retryable.
- Require an effective terminal runtime release event before retrying ambiguous
  null-compose failed legacy rows, even when `node_id` is stamped.
- Do not run AWF/GitHub-owned broad validation; use focused local checks only.

## Implementation Steps

1. Add the focused regression to `tests/unit/service/test_workspace_retry_port.py`.
2. Confirm the regression fails against the current retry guard.
3. Update `_source_runtime_not_yet_released` in
   `src/awf/service/workspaces_retry.py` so `node_id` does not prove a null
   compose terminal failed row has no runtime.
4. Re-run the focused regression and nearby retry-port tests needed to prove the
   preserved cases.
5. Record validation evidence in `plans/PRRT_kwDOSJAM6s6GX2bd_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_rejects_node_stamped_legacy_null_runtime_source_without_reservation -q`
  - Pass criterion: test fails before the production fix and passes afterward.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_allows_early_cancelled_source_without_runtime_evidence tests/unit/service/test_workspace_retry_port.py::test_retry_allows_when_source_compose_project_name_is_none tests/unit/service/test_workspace_retry_port.py::test_retry_rejects_legacy_null_runtime_source_without_reservation tests/unit/service/test_workspace_retry_port.py::test_retry_rejects_node_stamped_legacy_null_runtime_source_without_reservation -q`
  - Pass criterion: all selected retry gate cases pass.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
