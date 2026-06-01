# PRRT_kwDOSJAM6s6GPFz7 Plan

## Problem Statement and Scope

The PR review reports that `ComposeStackLauncher` skips the inline-build retry
when Docker Compose reports a missing pre-built companion image but the
`ComposeOperationError` is classified as `DOCKER_UNAVAILABLE`.

Scope is limited to `src/awf/node/stack_launcher.py`, focused regression tests
for companion image launch retry behavior, and this task's required plan and
validation documents.

## Requirements Checklist

- Add a regression test for a missing companion image error with
  `reason_code="DOCKER_UNAVAILABLE"`.
- Preserve non-companion missing image behavior: do not retry unless the missing
  image text names a current companion image tag.
- Preserve Docker-unavailable mapping for failures that do not match a
  companion image tag.
- Keep validation focused; broad AWF/GitHub validation remains managed by AWF
  after agent completion.

## Implementation Steps

1. Add a test variant in `tests/unit/node/test_stack_launcher_companion_images.py`
   for Docker-daemon-classified missing companion image output.
2. Update `_missing_prebuilt_companion_image_retry_spec` to detect missing
   companion image tags before returning `None` for Docker-unavailable failures.
3. Run the focused companion image stack-launcher test file.
4. Record validation evidence in `plans/PRRT_kwDOSJAM6s6GPFz7_VALIDATION.md`.
