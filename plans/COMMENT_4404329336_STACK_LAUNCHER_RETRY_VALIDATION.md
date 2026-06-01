# Comment 4404329336 Stack Launcher Retry Validation

Plan reference: `COMMENT_4404329336_STACK_LAUNCHER_RETRY_PLAN.md`

## Requirement Status

- Verify the review findings against current code before changing behavior:
  Complete. `stack_launcher.py` reused `spec` in the retry path and used a
  global plain `"not found"` match after only checking that the image appeared
  somewhere in compose output.
- Make the retry-spec intent explicit without changing successful retry
  behavior: Complete. Added a concise comment at the retry spec replacement.
- Preserve matching for `no such image`, `pull access denied`, and
  `repository does not exist`: Complete. Those branches remain unchanged.
- Avoid a standalone global `not found` match unless it is near the companion
  image tag: Complete. Plain `not found` now requires regex proximity to the
  escaped image tag within a 50-character window.
- Add focused regression coverage for the tightened `not found` heuristic:
  Complete. Added direct classifier tests for near-image and unrelated
  elsewhere `not found` output.
- Run only targeted validation for touched launcher behavior: Complete. Full
  AWF/GitHub validation is intentionally left to AWF after agent completion.

## Evidence

Changed files:

- `src/awf/node/stack_launcher.py`
- `tests/unit/node/test_stack_launcher_companion_images.py`
- `plans/COMMENT_4404329336_STACK_LAUNCHER_RETRY_PLAN.md`
- `plans/COMMENT_4404329336_STACK_LAUNCHER_RETRY_VALIDATION.md`

Commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q -k "missing_image_detector"`
  failed before the implementation change with the unrelated `not found`
  regression, as expected.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`
  passed: `13 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/stack_launcher.py tests/unit/node/test_stack_launcher_companion_images.py`
  passed.
