# Comment 4404329336 Stack Launcher Retry Plan

## Problem Statement and Scope

Address review-level feedback on `src/awf/node/stack_launcher.py` for the
pre-built companion image retry path. Keep the change limited to clarifying the
retry spec flow and tightening the missing-image heuristic so unrelated
`not found` messages do not trigger an inline-build retry.

## Requirements Checklist

- Verify the review findings against current code before changing behavior.
- Make the retry-spec intent explicit without changing the successful retry
  behavior.
- Preserve matching for `no such image`, `pull access denied`, and
  `repository does not exist`.
- Avoid a standalone global `not found` match unless it is near the companion
  image tag.
- Add focused regression coverage for the tightened `not found` heuristic.
- Run only targeted validation for the touched launcher tests; full AWF/GitHub
  validation remains managed by AWF after agent completion.

## Implementation Steps

1. Add a concise retry-path comment where `spec` is intentionally replaced by a
   revalidated retry spec.
2. Import `re` and update `_compose_up_reports_missing_image` so plain
   `not found` requires proximity to the image tag.
3. Add focused tests for near-image `not found` matching and unrelated
   `not found` text that still mentions the image elsewhere.
4. Run the targeted companion-image launcher tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`
  passes.
