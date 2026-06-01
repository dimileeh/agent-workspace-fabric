# Address PRRT_kwDOSJAM6s6GPTGa Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6GPTGa` reports that missing-image retry
revalidation in `src/awf/node/stack_launcher.py` can raise a raw
`ComposeOperationError` when Docker becomes unavailable. The first launch-time
revalidation path already maps `DOCKER_UNAVAILABLE` to
`WorkspaceServiceExecutionError`; the retry path should use the same workspace
launch error shape.

Scope is limited to stack launcher retry-time revalidation behavior and its
focused unit coverage.

## Requirements Checklist

- Add a regression test that fails when retry-time companion image revalidation
  raises `ComposeOperationError(reason_code="DOCKER_UNAVAILABLE")` and expects
  `WorkspaceServiceExecutionError`.
- Preserve the missing-image retry behavior that clears the originally missing
  companion image and revalidates any remaining prebuilt companion images.
- Map retry-time Docker-unavailable revalidation failures with the same helper
  used for compose-up Docker-unavailable failures.
- Keep changes scoped to implementation, focused tests, and plan/validation
  artifacts.
- Run only focused validation; AWF/GitHub owns broad validation after agent
  completion.

## Implementation Steps

1. Inspect the retry branch and current companion image launcher tests.
2. Add a focused regression test for Docker-unavailable retry revalidation.
3. Run the new test and confirm it fails with a raw `ComposeOperationError`.
4. Update the retry branch so retry revalidation and retry compose-up share the
   same `ComposeOperationError` mapping.
5. Run focused tests for the changed behavior.
6. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`
  - Passes after implementation.
  - Initial run of the new regression should fail before implementation.

Full AWF/GitHub validation is intentionally not run during the agent phase per
the workspace contract.
