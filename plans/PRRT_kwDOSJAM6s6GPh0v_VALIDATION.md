# PRRT_kwDOSJAM6s6GPh0v Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6GPh0v_PLAN.md`

## Requirement Status

- Complete: Added a regression test for repeated missing prebuilt companion
  image failures across multiple compose-up attempts.
- Complete: Retries are bounded by the count of prebuilt companion images still
  present after launch-time revalidation.
- Complete: Existing Docker-unavailable mapping and non-companion missing-image
  behavior are preserved by the focused companion-image launcher test module.
- Complete: Work is prepared for a local commit on the current AWF-managed
  branch; no branch switch or push was performed.

## Evidence

Files changed:

- `src/awf/node/stack_launcher.py`
- `tests/unit/node/test_stack_launcher_companion_images.py`
- `plans/PRRT_kwDOSJAM6s6GPh0v_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6GPh0v_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py::test_launch_retries_repeated_missing_prebuilt_companion_images -q`
  failed before implementation with the second missing prebuilt companion image
  re-raised as `ComposeOperationError`.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py::test_launch_retries_repeated_missing_prebuilt_companion_images -q`
  passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`
  passed after implementation: 17 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/stack_launcher.py tests/unit/node/test_stack_launcher_companion_images.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after completion.
