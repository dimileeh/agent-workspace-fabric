# Review Thread PRRT_kwDOSJAM6s6CLdGt Validation

Plan reference: `review_thread_PRRT_kwDOSJAM6s6CLdGt_PLAN.md`

## Requirement Status

- Add a regression test proving `drain_due` dispatches callback target validation
  through `asyncio.to_thread`: Complete.
  - Evidence: `tests/unit/service/test_callbacks.py` adds
    `test_drain_due_offloads_callback_target_validation`.
  - TDD evidence: the new test failed before implementation with
    `assert 0 == 1` for recorded `to_thread` calls.
- Preserve existing invalid-target handling as `CALLBACK_TARGET_INVALID`:
  Complete.
  - Evidence: existing callback service tests still pass, including invalid
    delivery target policy coverage.
- Preserve successful delivery behavior, including passing the validated connect
  IP address to the HTTP poster: Complete.
  - Evidence: the new regression asserts the poster receives `1.1.1.1`, and
    the existing successful delivery test still passes.
- Keep changes scoped to callback delivery service code, tests, and plan
  workflow documentation: Complete.
  - Evidence: changed files are `src/awf/service/callbacks.py`,
    `tests/unit/service/test_callbacks.py`, this validation file, and the plan
    file.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_drain_due_offloads_callback_target_validation -q`
  - Failed before implementation as expected.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  - Passed: 15 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Gaps

None.
