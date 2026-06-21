# Post-Validation Mirror Hooks Repair Validation

Plan reference: `POST_VALIDATION_MIRROR_HOOKS_REPAIR_PLAN.md`

## Requirement Status

- Add post-validation mirror hooks repair after validation commands and cleanup:
  Complete. `_run_pre_push_validation` now repairs the mirror after validation
  exception, cleanup failure, and final validation result paths.
- Cover validation failure and cleanup failure returns:
  Complete. Added focused regressions for failed validation, failed post-
  validation repair, and validation cleanup failure.
- Fail closed with the existing mirror-hooks poisoned reason if post-validation
  repair fails:
  Complete. A failed post-validation repair returns
  `MIRROR_HOOKS_PATH_POISONED`.
- Preserve existing pre-validation repair behavior:
  Complete. Existing pre-validation repair remains unchanged; focused edge file
  still passes.
- Add focused regression tests without broad AWF/GitHub validation:
  Complete. Only targeted pytest and ruff commands were run locally.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`
- `plans/POST_VALIDATION_MIRROR_HOOKS_REPAIR_PLAN.md`
- `plans/POST_VALIDATION_MIRROR_HOOKS_REPAIR_VALIDATION.md`

Focused checks:

- Initial regression run failed as expected:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q -k 'post_validation_mirror or repairs_mirror_hooks_after_validation_failure or repairs_mirror_hooks_after_cleanup_failure'`
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q -k 'post_validation_mirror or repairs_mirror_hooks_after_validation_failure or repairs_mirror_hooks_after_cleanup_failure'`
  passed with 3 passed, 13 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q`
  passed with 16 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py -q -k first_party_code_files_stay_under_line_limit`
  passed with 1 passed, 8 deselected.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after agent completion.
