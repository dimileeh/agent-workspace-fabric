# Request Admission Verified Bearer Plan

## Problem Statement and Scope

The request admission helper currently uses any syntactically valid
`Authorization: Bearer ...` header as a quota identity. Public callback
registration can therefore be sharded by rotating unverified bearer values.

Scope is limited to request admission identity selection and the local API token
dependency that can mark requests as verified. No route authorization policy is
changed.

## Requirements Checklist

- Unverified bearer headers must fall back to client-host admission identity.
- Bearer-token admission identity must remain available after upstream local API
  token verification.
- Protected workspace create endpoints must keep their current bearer-token
  admission behavior after `require_api_token` succeeds.
- Public callback registration must not let rotated bearer values bypass the
  configured per-host limit.
- Admission metadata must remain redacted and must not include raw bearer tokens.
- Keep changes narrowly scoped and covered by unit regressions.

## Implementation Steps

1. Update request admission tests to cover unverified bearer fallback and
   verified bearer identity.
2. Update callback tests to assert unverified bearer rotation is bounded by the
   client-host bucket.
3. Add a small verified-auth request marker set by `require_api_token` after a
   successful comparison.
4. Gate bearer-token identity extraction on that verified-auth marker.
5. Run focused unit tests, then lint/typecheck for touched Python code.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_callbacks.py tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rejects_v1_create_burst_after_configured_limit tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2DiskPressure::test_rejects_v2_create_burst_after_configured_limit -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/auth_context.py src/awf/api/deps.py src/awf/api/request_admission.py tests/unit/api/test_deps.py tests/unit/api/test_callbacks.py`
  must pass.
- `uv run --python 3.12 --extra dev mypy src/awf`
  must pass.
