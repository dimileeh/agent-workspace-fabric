# PRRT_kwDOSJAM6s6GPwu4 Compose Image Ref Colon Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6GPwu4` reports that the exact image-reference
regex used by `_compose_up_reports_missing_image` treats `:` as a trailing image
reference character. That makes the fallback pattern for Docker text shaped like
`<tag>: no such image` unreachable for normal Docker output, because the colon
separator fails the trailing negative lookahead.

Scope is limited to the missing-prebuilt-companion image classifier in
`src/awf/node/stack_launcher.py` and focused regression coverage in
`tests/unit/node/test_stack_launcher_companion_images.py`.

## Requirements Checklist

- Add a regression showing `<companion-tag>: no such image` is classified as a
  missing companion image.
- Preserve existing exact-match protections for unrelated images and generic
  not-found text near a companion tag.
- Update the regex composition so the colon separator form is reachable without
  broadening the other missing-image patterns.
- Run only focused validation for the touched Python files and tests.
- Commit the fix locally with a conventional commit referencing the thread id.

## Implementation Steps

1. Add the focused regression to
   `tests/unit/node/test_stack_launcher_companion_images.py`.
2. Run the new regression before implementation when practical to confirm the
   current false negative.
3. Add a colon-delimited exact image-reference helper in
   `src/awf/node/stack_launcher.py` and use it only for the
   `<tag>: no such image` pattern.
4. Re-run the focused companion-image classifier tests and focused lint for the
   touched files.
5. Record validation evidence in
   `plans/PRRT_kwDOSJAM6s6GPwu4_COMPOSE_IMAGE_REF_COLON_VALIDATION.md`.
