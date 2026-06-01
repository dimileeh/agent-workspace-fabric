# Retry Companion Image Revalidation Plan

## Problem Statement and Scope

PR thread `PRRT_kwDOSJAM6s6GPF0A` reports that `ComposeStackLauncher.launch` revalidates pre-built companion images before the first `compose.up`, but skips that revalidation before the missing-image retry. If one companion tag triggers the retry and another cached companion tag is pruned between the first attempt and retry, the retry can keep rendering that second stale image with `pull_policy: never`.

Scope is limited to the missing-prebuilt-companion retry path in `src/awf/node/stack_launcher.py` and focused unit coverage in `tests/unit/node/test_stack_launcher_companion_images.py`.

## Requirements Checklist

- [ ] Add a regression test showing the retry path revalidates still-image-backed companions after a missing pre-built image failure.
- [ ] Keep the existing missing-image retry behavior that clears the tag reported by `compose.up`.
- [ ] Re-run prebuilt companion image revalidation before the retry `compose.up` so other vanished cache-hit tags fall back to inline build.
- [ ] Preserve targeted validation only; do not run broad AWF/GitHub-owned validation suites.
- [ ] Commit the fix locally with a conventional commit referencing the thread id.

## Implementation Steps

1. Add a two-companion unit test where initial revalidation succeeds for both cached tags, the first `compose.up` reports one tag missing, and retry-time revalidation finds the other tag missing.
2. Confirm the new test fails before implementation when practical.
3. Update `ComposeStackLauncher.launch` so the retry spec from `_missing_prebuilt_companion_image_retry_spec` is passed through `_revalidate_prebuilt_companion_images` before the second `compose.up`.
4. Run the focused companion-image test module or narrower test selection.
5. Record validation evidence in `plans/RETRY_COMPANION_IMAGE_REVALIDATION_VALIDATION.md` and commit only changed files.
