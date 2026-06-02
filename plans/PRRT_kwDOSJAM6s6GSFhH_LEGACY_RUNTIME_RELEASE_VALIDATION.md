# Legacy Runtime Release Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6GSFhH_LEGACY_RUNTIME_RELEASE_PLAN.md`

## Requirement Status

- Add a regression test for successful null-locator legacy destroy cleanup:
  Complete. Added
  `test_destroy_legacy_null_locator_records_runtime_released_after_cleanup`.
- Preserve stored-locator release behavior:
  Complete. The focused release-event test selection still covers the existing
  compose-file-only and normal partial-cleanup cases.
- Preserve partial-cleanup release behavior when `compose_down` succeeds:
  Complete. Added
  `test_destroy_legacy_null_locator_partial_cleanup_records_runtime_released`
  and kept the existing stored-locator partial-cleanup test passing.
- Keep validation focused:
  Complete. Ran only targeted lifecycle tests and focused ruff for touched
  Python files. Full AWF/GitHub validation remains managed by AWF after agent
  completion.

## Evidence

Files changed:

- `src/awf/service/controls.py`
- `tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_002.py`
- `plans/PRRT_kwDOSJAM6s6GSFhH_LEGACY_RUNTIME_RELEASE_PLAN.md`

Commands:

- Pre-fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_002.py -q -k legacy_null_locator`
  failed with both new legacy null-locator tests missing
  `workspace.terminal_runtime_released`.
- Post-fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_002.py -q -k legacy_null_locator`
  passed: 2 passed, 24 deselected.
- Post-fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_002.py -q -k "runtime_released or compose_file_only"`
  passed: 4 passed, 22 deselected.
- Post-fix:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_002.py`
  passed.

## Gaps

No planned gaps remain.
