# PRRT_kwDOSJAM6s6CONq1 Plan

## Problem Statement and Scope

The PR review thread reports that `require_websocket_api_token` delegates
WebSocket handshake authentication failures to `_require_authorization_header`,
which always raises `HTTPException`. The scope is limited to making WebSocket
auth failures use WebSocket-compatible denial behavior while preserving the
existing REST auth contract and the structured WebSocket denial response contract
where the ASGI server supports WebSocket HTTP denial responses.

## Requirements Checklist

- Add regression coverage proving WebSocket auth falls back to
  `WebSocketException` when the WebSocket HTTP denial extension is unavailable.
- Keep current WebSocket TestClient behavior for supported denial responses:
  invalid or missing handshake auth returns a structured `401 UNAUTHORIZED`, and
  missing server token config returns structured `503 API_TOKEN_NOT_CONFIGURED`.
- Keep REST `require_api_token` behavior unchanged.
- Keep the change local to the review thread.

## Implementation Steps

1. Add focused dependency tests for unsupported WebSocket denial-extension
   auth failures and update the existing successful WebSocket helper test for an
   async dependency.
2. Run the new dependency test before implementation and confirm it fails
   against the current HTTP-only WebSocket helper.
3. Refactor auth validation so REST still raises `HTTPException`, while the
   WebSocket dependency chooses a WebSocket-specific structured denial exception
   only when supported and `WebSocketException` close-code fallback otherwise.
4. Register the structured denial exception handler on the FastAPI app so
   supported WebSocket handshakes receive the existing JSON denial body.
5. Run focused dependency and WebSocket route tests, then lint the touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py::test_require_websocket_api_token_uses_websocket_exception_without_denial_extension -q`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py tests/unit/api/test_observability_api.py::TestWorkspaceWebSocket::test_websocket_requires_token tests/unit/api/test_observability_api.py::TestWorkspaceWebSocket::test_websocket_reports_missing_token_configuration -q`
  passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/deps.py tests/unit/api/test_deps.py`
  passes.
