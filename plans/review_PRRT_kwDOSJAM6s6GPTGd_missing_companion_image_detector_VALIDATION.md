# Missing Companion Image Detector Review Fix Validation

Plan reference:
`plans/review_PRRT_kwDOSJAM6s6GPTGd_missing_companion_image_detector_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving that mentioning the companion tag
  elsewhere in Compose output does not trigger retry when the missing-image
  phrase names `postgres:16`.
- Complete: Preserved retry behavior when Docker explicitly reports the
  specific companion tag with `No such image: <tag>`.
- Complete: Kept validation focused to the changed detector behavior. Full
  AWF/GitHub validation is managed after agent completion.

## Evidence

Files changed:

- `src/awf/node/stack_launcher.py`
- `tests/unit/node/test_stack_launcher_companion_images.py`
- `plans/review_PRRT_kwDOSJAM6s6GPTGd_missing_companion_image_detector_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6GPTGd_missing_companion_image_detector_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py::test_missing_image_detector_rejects_other_image_when_companion_tag_is_elsewhere -q`
  - Expected pre-fix failure observed.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`
  - Passed: `16 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/stack_launcher.py tests/unit/node/test_stack_launcher_companion_images.py`
  - Passed.

## Gaps

None.
