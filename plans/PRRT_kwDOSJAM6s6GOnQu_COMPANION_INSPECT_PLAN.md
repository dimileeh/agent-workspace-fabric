# PRRT_kwDOSJAM6s6GOnQu Companion Inspect Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6GOnQu` reports that
`CompanionImageBuilder.companion_image_exists()` calls the private
`ComposeManager._docker_capture()` method directly and suppresses `SLF001`.
The fix should preserve the stricter launch-time behavior that treats confirmed
missing images as absent while surfacing other Docker inspect failures.

Scope is limited to the companion image inspect API and focused unit coverage.

## Requirements Checklist

- Add a public `ComposeManager` method for strict companion image inspection.
- Preserve existing lenient `ComposeManager.companion_image_exists()` behavior:
  any inspect failure returns `False`.
- Preserve builder launch-time behavior: confirmed missing image returns `False`,
  but non-missing inspect failures raise with the original reason code.
- Remove the builder's direct private `_docker_capture()` access and `SLF001`
  suppression.
- Validate with focused tests and lint only for touched code; broad AWF/GitHub
  validation remains managed after agent completion.

## Implementation Steps

1. Update tests first so `CompanionImageBuilder` relies on a public strict
   inspect method and `ComposeManager` covers the strict behavior.
2. Run the focused new/changed tests and confirm the expected failure.
3. Add `ComposeManager.companion_image_inspect()` and shared missing-image
   classification.
4. Update `companion_images.py` to call the public strict method.
5. Run focused unit tests and narrow lint checks for the touched files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_compose_manager.py::TestCompanionImageCommands tests/unit/node/test_companion_images.py::test_companion_image_exists_returns_true_when_tag_present tests/unit/node/test_companion_images.py::test_companion_image_exists_returns_false_when_tag_missing tests/unit/node/test_companion_images.py::test_companion_image_exists_treats_docker_unavailable_no_such_image_as_missing tests/unit/node/test_companion_images.py::test_companion_image_exists_preserves_probe_error_reason_code tests/unit/node/test_companion_images.py::test_companion_image_exists_preserves_unexpected_inspect_failure -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/compose_manager.py src/awf/node/companion_images.py tests/unit/node/test_compose_manager.py tests/unit/node/test_companion_images.py`
  passes.
