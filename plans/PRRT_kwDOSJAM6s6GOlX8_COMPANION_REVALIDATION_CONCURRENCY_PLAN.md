# PRRT_kwDOSJAM6s6GOlX8 Companion Revalidation Concurrency Plan

## Problem Statement and Scope

The PR review thread reports that launch-time revalidation of pre-built companion
images probes image existence sequentially. Multiple companion image probes are
independent, so sequential awaits can add unnecessary workspace provisioning
latency. Scope is limited to `ComposeStackLauncher` companion image revalidation
and focused regression coverage.

## Requirements Checklist

- Revalidate companion images concurrently when a builder is configured.
- Preserve existing behavior for companions without an image, existing images,
  missing images, and unchanged specs.
- Add a regression test that fails if image-existence probes are dispatched
  sequentially.
- Run only focused validation owned by this change; full AWF/GitHub validation
  remains managed after agent completion.

## Implementation Steps

1. Add a focused async barrier regression test in
   `tests/unit/node/test_stack_launcher_companion_images.py`.
2. Confirm the new test fails against the current sequential implementation.
3. Update `_revalidate_prebuilt_companion_images` to dispatch independent image
   probes with `asyncio.gather`.
4. Re-run the focused test module or individual relevant tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`
  passes.
- If run before implementation, the new concurrency test times out/fails,
  demonstrating the regression coverage.
