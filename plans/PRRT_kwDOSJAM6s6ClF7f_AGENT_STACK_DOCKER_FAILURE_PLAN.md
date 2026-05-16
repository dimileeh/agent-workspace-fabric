# PRRT_kwDOSJAM6s6ClF7f Agent Stack Docker Failure Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6ClF7f` reports that `ComposeStackLauncher.launch()` returns
`None` when Docker is unavailable for a profile with no profile-declared required services.
That still means the always-required agent compose stack failed to start, and provisioning
must fail with the original Docker startup cause instead of marking the workspace ready.

Scope is limited to the stack launcher behavior and focused unit coverage.

## Requirements Checklist

- Docker unavailability during compose `up` must fail launch even when the profile declares
  no required services.
- Existing required service diagnostics must be preserved for serviceful and dind profiles.
- Non-Docker compose failures must keep their existing propagation behavior.
- Add or update a regression test that fails before the implementation change.
- Stage and commit only files changed for this thread.

## Implementation Steps

1. Update the no-required-services stack launcher test to expect
   `WorkspaceServiceExecutionError` instead of `None`.
2. Run the focused test to confirm it fails against the current implementation.
3. Change `ComposeStackLauncher.launch()` so `DOCKER_UNAVAILABLE` always raises
   `WorkspaceServiceExecutionError`, with a clear message for the agent-only case.
4. Run focused stack launcher tests.
5. Run lint for the touched Python surface if practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/stack_launcher.py tests/unit/node/test_stack_launcher.py`
  passes.
