# PRRT_kwDOSJAM6s6GPh0v Plan

## Problem Statement And Scope

The PR review thread reports that `ComposeStackLauncher.launch` retries only one
compose-up missing-image race for prebuilt companion images. A workspace with
multiple prebuilt companions can still fail if a different companion tag is
pruned after retry-time revalidation and before the next `docker compose up`.

Scope is limited to the stack launcher missing-prebuilt retry behavior and its
unit coverage.

## Requirements Checklist

- Add a regression test for repeated missing prebuilt companion image failures
  across multiple compose-up attempts.
- Keep retries bounded by the number of prebuilt companion images still present
  after launch-time revalidation.
- Preserve existing behavior for Docker-unavailable error mapping and for
  non-companion missing images.
- Commit the fix locally without pushing or switching branches.

## Implementation Steps

1. Add a focused unit test in `tests/unit/node/test_stack_launcher_companion_images.py`
   that fails with the current single-retry implementation.
2. Refactor `ComposeStackLauncher.launch` to loop missing-prebuilt retries until
   the bounded retry budget is exhausted or compose-up succeeds.
3. Run the targeted regression test first, then the focused companion-image
   stack-launcher test module.
4. Record validation evidence in `plans/PRRT_kwDOSJAM6s6GPh0v_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py::<new-test> -q`
  must fail before implementation and pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`
  must pass after implementation.
- Full AWF/GitHub validation is intentionally not run in the agent phase.
