# PR260 Workspace Helper Provider Readiness Plan

## Problem statement and scope

PR #260 CI reports `python-full-coverage` failures in workspace REST list and
identity tests. The focused nodes pass locally when the agent container has
ambient Claude/Gemini auth, but fail with 409 provider-readiness conflicts when
that ambient auth is removed. These tests use `_create_workspace` as a fixture
helper for unrelated API behavior, so they should not depend on host auth for
non-Codex agents.

Scope is limited to test fixture setup. Production provider-readiness behavior
and dedicated provider-readiness tests must remain unchanged.

## Requirements checklist

- Reproduce the reported focused failures in an auth-sanitized environment.
- Preserve provider-readiness enforcement in production code.
- Make `_create_workspace` fixture creates independent of ambient provider auth.
- Keep direct tests that already pass unchanged unless a focused repro shows
  they still fail.
- Verify the sanitized focused repro passes after the patch.
- Write validation evidence and commit the fix locally without pushing.

## Implementation steps

1. Update the shared `_create_workspace` test helper in
   `tests/unit/api/test_workspaces.py` to build payloads through the existing
   `_v2_body_with_preflight_override` helper.
2. Run the auth-sanitized focused repro from the CI evidence.
3. Run the normal focused repro and a dedicated provider-readiness blocking test
   to confirm the check was not disabled.

## Verification commands and pass criteria

- Auth-sanitized focused repro for the four reported node IDs passes.
- Normal focused repro for the four reported node IDs passes.
- `test_v2_create_blocks_missing_selected_provider_readiness` still passes.
