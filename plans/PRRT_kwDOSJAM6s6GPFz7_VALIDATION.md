# PRRT_kwDOSJAM6s6GPFz7 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6GPFz7_PLAN.md`

## Requirement Status

- Complete: Added a regression test for a missing companion image error with
  `reason_code="DOCKER_UNAVAILABLE"`.
- Complete: Preserved non-companion missing image behavior by retaining the
  concrete companion tag match requirement before retrying.
- Complete: Preserved Docker-unavailable mapping for failures that do not match
  a companion image tag; only matching companion image errors now retry first.
- Complete: Kept validation focused. Full AWF/GitHub validation is managed by
  AWF after agent completion.

## Evidence

Files changed:

- `src/awf/node/stack_launcher.py`
- `tests/unit/node/test_stack_launcher_companion_images.py`
- `plans/PRRT_kwDOSJAM6s6GPFz7_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6GPFz7_VALIDATION.md`

Commands run:

- Expected failing regression before production fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py::test_launch_retries_daemon_classified_missing_prebuilt_companion_image -q`
- Passing focused regression suite:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/node/stack_launcher.py tests/unit/node/test_stack_launcher_companion_images.py`

No remaining gaps.
