# PRRT_kwDOSJAM6s6GPwu4 Compose Image Ref Colon Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6GPwu4_COMPOSE_IMAGE_REF_COLON_PLAN.md`

## Requirement Status

- Add a regression showing `<companion-tag>: no such image` is classified as a
  missing companion image: Complete.
- Preserve existing exact-match protections for unrelated images and generic
  not-found text near a companion tag: Complete.
- Update the regex composition so the colon separator form is reachable without
  broadening the other missing-image patterns: Complete.
- Run only focused validation for the touched Python files and tests: Complete.
- Commit the fix locally with a conventional commit referencing the thread id:
  Complete.

## Evidence

Files changed:

- `src/awf/node/stack_launcher.py`
- `tests/unit/node/test_stack_launcher_companion_images.py`
- `plans/PRRT_kwDOSJAM6s6GPwu4_COMPOSE_IMAGE_REF_COLON_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6GPwu4_COMPOSE_IMAGE_REF_COLON_VALIDATION.md`

Focused checks:

- Confirmed the new regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q -k tag_colon_no_such_image`
- Passed focused missing-image classifier tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q -k missing_image_detector`
- Passed focused companion-image stack launcher tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`
- Passed focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/node/stack_launcher.py tests/unit/node/test_stack_launcher_companion_images.py`

Full AWF/GitHub-owned validation was not run inside the agent phase per the
workspace contract.
