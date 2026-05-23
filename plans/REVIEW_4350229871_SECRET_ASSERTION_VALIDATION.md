# Review 4350229871 Secret Assertion Validation

Plan reference: `plans/REVIEW_4350229871_SECRET_ASSERTION_PLAN.md`

## Requirement Status

- Complete: Replaced the fragile generic `"root"` output assertion with a
  targeted assertion that `AWF_API_TOKEN=root` is not leaked.
- Complete: Preserved the existing merge-failure regression assertions in
  `tests/unit/cli/test_init.py`.
- Complete: Kept changes scoped to the affected test plus required
  plan/validation docs.
- Complete: Ran only focused validation for the affected behavior.

## Evidence

Files changed:

- `tests/unit/cli/test_init.py`
- `plans/REVIEW_4350229871_SECRET_ASSERTION_PLAN.md`
- `plans/REVIEW_4350229871_SECRET_ASSERTION_VALIDATION.md`

Focused validation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q -k test_init_without_path_json_marks_non_utf8_env_overlay_merge_failed
```

Result: passed, `1 passed, 135 deselected`.

Full AWF/GitHub validation, coverage gates, and CI-equivalent checks are
managed by AWF after agent completion for this repair cycle.
