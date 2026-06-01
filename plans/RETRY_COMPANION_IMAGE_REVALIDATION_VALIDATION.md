# Retry Companion Image Revalidation Validation

Plan reference: `plans/RETRY_COMPANION_IMAGE_REVALIDATION_PLAN.md`

## Requirement Status

- Complete: Add a regression test showing the retry path revalidates still-image-backed companions after a missing pre-built image failure.
- Complete: Keep the existing missing-image retry behavior that clears the tag reported by `compose.up`.
- Complete: Re-run prebuilt companion image revalidation before the retry `compose.up` so other vanished cache-hit tags fall back to inline build.
- Complete: Preserve targeted validation only; broad AWF/GitHub-owned validation suites were not run.
- Complete: Commit the fix locally with a conventional commit referencing the thread id.

## Evidence

Files changed:

- `src/awf/node/stack_launcher.py`
- `tests/unit/node/test_stack_launcher_companion_images.py`
- `plans/RETRY_COMPANION_IMAGE_REVALIDATION_PLAN.md`
- `plans/RETRY_COMPANION_IMAGE_REVALIDATION_VALIDATION.md`

Focused checks:

- Before implementation, the new regression failed as expected:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py::test_launch_revalidates_remaining_prebuilt_companion_images_before_retry -q`
- After implementation, the focused companion-image module passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`
- Touched-file lint passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/node/stack_launcher.py tests/unit/node/test_stack_launcher_companion_images.py`

Full AWF/GitHub validation, provenance, and merge gating are managed by AWF after agent completion and were intentionally not executed in this workspace phase.
