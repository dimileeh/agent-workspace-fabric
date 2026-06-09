# Review 4664994375 Duplicate Stale Diagnostic Validation

Plan reference: `plans/REVIEW_4664994375_DUPLICATE_STALE_PLAN.md`

## Requirement Status

- Verify the duplicate-plus-stale claim against the local implementation before
  editing: Complete.
  - Evidence: reviewed `scripts/ci/check_j2_tojson.py`; duplicate directives
    were skipped while building `allow_by_expression`, but stale detection still
    iterated the full `allow_directives` list.
- Add a regression test where duplicate allowlist directives target an
  expression that is now escaped: Complete.
  - Evidence: added
    `test_checker_does_not_mark_duplicate_allowlist_entry_stale`.
- Ensure duplicate directives are not also reported as stale on the same line:
  Complete.
  - Evidence: the new regression asserts the duplicate line has a duplicate
    diagnostic and no stale diagnostic.
- Preserve stale diagnostics for the canonical allowlist directive when the
  expression is no longer used raw: Complete.
  - Evidence: the new regression asserts the first directive still receives a
    stale allowlist diagnostic.
- Keep existing duplicate, stale, allowlist, and escaping behavior intact:
  Complete.
  - Evidence: focused checker test file passed after the fix.
- Run only targeted checker tests: Complete.
  - Evidence: ran the focused checker test file and focused ruff command only.
    Full AWF/GitHub validation is managed after agent completion.
- Commit the scoped fix locally without pushing or switching branches: Complete.
  - Evidence: this scoped change set is committed locally for AWF to push after
    agent completion.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_j2_tojson.py -q`
  - First run after adding the regression: failed with `1 failed, 9 passed`,
    confirming the duplicate line also received a stale diagnostic.
  - Final run after implementation: passed, `10 passed in 2.56s`.
- `uv run --python 3.12 --extra dev ruff check scripts/ci/check_j2_tojson.py tests/unit/scripts/test_check_j2_tojson.py`
  - Passed.

## Files Changed

- `scripts/ci/check_j2_tojson.py`
- `tests/unit/scripts/test_check_j2_tojson.py`
- `plans/REVIEW_4664994375_DUPLICATE_STALE_PLAN.md`
- `plans/REVIEW_4664994375_DUPLICATE_STALE_VALIDATION.md`

## Gaps

None. Broad validation was intentionally not run inside the agent phase per the
AWF workspace contract.
