# PRRT_kwDOSJAM6s6CONq1 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6CONq1_PLAN.md`

## Requirement Status

- Complete: Added regression coverage proving WebSocket auth falls back to
  `WebSocketException` when the WebSocket HTTP denial extension is unavailable.
  Evidence: `tests/unit/api/test_deps.py` includes
  `test_require_websocket_api_token_uses_websocket_exception_without_denial_extension`.
- Complete: Preserved current WebSocket TestClient behavior for supported
  structured denial responses. Evidence: the dependency now raises
  `WebSocketAuthorizationDenialError` for supported denial responses, the app
  registers its handler, and focused route tests for invalid auth and missing
  token configuration still pass with `WebSocketDenialResponse`.
- Complete: REST `require_api_token` behavior is unchanged. Evidence:
  `tests/unit/api/test_deps.py` passes, including existing REST auth tests.
- Complete: Change is local to the review thread. Evidence: implementation
  changes are limited to `src/awf/api/deps.py`, the app-level exception handler
  registration in `src/awf/api/app.py`, focused dependency tests, and this
  plan/validation pair.

## Verification Evidence

- Pre-fix regression check:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py::test_require_websocket_api_token_uses_websocket_exception_without_denial_extension -q`
  failed because the WebSocket dependency raised `HTTPException` for missing,
  wrong, and server-unconfigured auth failures when no denial extension was
  available.
- Focused dependency check:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py::test_require_websocket_api_token_uses_websocket_exception_without_denial_extension tests/unit/api/test_deps.py::test_require_websocket_api_token_reads_handshake_authorization_header -q`
  passed with 4 tests.
- Planned focused auth check:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_observability_api.py::TestWorkspaceWebSocket::test_websocket_requires_token tests/unit/api/test_observability_api.py::TestWorkspaceWebSocket::test_websocket_reports_missing_token_configuration -q`
  passed with 18 tests.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/api/deps.py src/awf/api/app.py tests/unit/api/test_deps.py`
  passed.
- Type check:
  `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Additional Observation

- Broader exploratory command
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_observability_api.py::TestWorkspaceWebSocket -q`
  had 19 passing tests and 1 unrelated failure:
  `test_log_api_includes_monitor_and_recovery_streams` received `503 Service
  Unavailable` while creating a workspace before the test adds auth headers.
  The focused WebSocket auth route tests above passed.

## Gaps

None.
