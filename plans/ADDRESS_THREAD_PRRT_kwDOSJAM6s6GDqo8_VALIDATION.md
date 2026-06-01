# Address PRRT_kwDOSJAM6s6GDqo8 Validation

Plan reference: `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6GDqo8_PLAN.md`

## Requirement Status

- Add a regression test proving unknown-only `allowed_merge_methods` is treated
  as unconstrained: Complete.
- Preserve existing behavior for recognized methods and for intersections of
  recognized branch rules: Complete.
- Keep validation focused to the changed parser tests; AWF/GitHub own broad
  validation after agent completion: Complete.
- Commit the thread-specific fix locally without pushing: Complete after the
  local commit for this change.

## Evidence

Files changed:

- `src/awf/common/github_client.py`
- `tests/unit/common/test_github_client_parts/test_github_client_part_004.py`
- `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6GDqo8_PLAN.md`
- `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6GDqo8_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py -q -k "unknown_only_unconstrained"`
  - Expected pre-fix result: failed because the parser returned `()` instead
    of `None`.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py -q -k "allowed_merge_methods"`
  - Result: passed, `10 passed, 35 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client_parts/test_github_client_part_004.py`
  - Result: passed.

Broad AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after agent completion.

## Gaps

None.
