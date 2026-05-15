# Request Admission Verified Bearer Validation

Plan reference: `REQUEST_ADMISSION_VERIFIED_BEARER_PLAN.md`

## Requirement Status

- Complete: Unverified bearer headers fall back to client-host admission
  identity.
  - Evidence: `tests/unit/api/test_deps.py`
    `test_request_admission_unverified_bearer_falls_back_to_client_host`.
- Complete: Bearer-token admission identity remains available after upstream
  local API token verification.
  - Evidence: `src/awf/api/deps.py` marks verified requests;
    `src/awf/api/request_admission.py` gates bearer identity on the marker;
    `tests/unit/api/test_deps.py`
    `test_request_admission_verified_bearer_identity_is_digest_only` and
    `test_require_api_token_marks_request_as_verified_on_success`.
- Complete: Protected workspace create endpoints keep bearer-token admission
  behavior after `require_api_token` succeeds.
  - Evidence:
    `tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rejects_v1_create_burst_after_configured_limit`
    and
    `tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2DiskPressure::test_rejects_v2_create_burst_after_configured_limit`.
- Complete: Public callback registration cannot bypass limits by rotating
  unverified bearer values.
  - Evidence: `tests/unit/api/test_callbacks.py`
    `test_register_callback_uses_client_host_for_unverified_bearer_identity`
    and
    `test_register_callback_bounds_rotated_unverified_bearers_by_client_host`.
- Complete: Admission metadata remains redacted.
  - Evidence: Existing redaction assertions were preserved and updated
    callback rejection assertions still check raw tokens are absent.
- Complete: Changes are narrowly scoped and covered by unit regressions.
  - Evidence: files changed are limited to request auth context/admission,
    auth dependency, focused API tests, and required plan/validation artifacts.

## Verification Commands

- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_callbacks.py::test_register_callback_uses_client_host_for_unverified_bearer_identity tests/unit/api/test_callbacks.py::test_register_callback_bounds_rotated_unverified_bearers_by_client_host tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rejects_v1_create_burst_after_configured_limit tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2DiskPressure::test_rejects_v2_create_burst_after_configured_limit -q`
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/api/auth_context.py src/awf/api/deps.py src/awf/api/request_admission.py tests/unit/api/test_deps.py tests/unit/api/test_callbacks.py`
- Passed:
  `uv run --python 3.12 --extra dev mypy src/awf`

## Gaps

None.
