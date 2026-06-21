# PR614 Full Coverage 2026-06-20 Validation

Plan reference: `plans/PR614_FULL_COVERAGE_20260620_PLAN.md`

## Requirement Status

- Diagnose the failing CI check from Actions evidence and coverage artifact:
  Complete.
  - GitHub Actions run `27861705439` showed all `python-coverage-shards`
    jobs passed.
  - `python-full-coverage` failed while combining shards with total coverage
    `98.93`, below the `99.00` threshold.
  - Downloaded `full-coverage-report` and parsed `coverage.xml`; uncovered
    PR-touched helper branches included `src/awf/control/executor/helpers.py`.

- Add or update focused tests that assert real behavior:
  Complete.
  - Added
    `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_014.py`.
  - Tests assert snapshot profile realignment, PR-monitor recovery model
    selection, failure reason mapping, and coverage failure messages.

- Keep changes scoped:
  Complete.
  - No production code, workflow, threshold, or quality-gate configuration was
    changed.

- Run focused verification:
  Complete.
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_014.py -q`
    - Passed: 11 tests.
  - `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_014.py`
    - Passed.
  - `uv run --python 3.12 --extra dev coverage erase && uv run --python 3.12 --extra dev coverage run -m pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_014.py -q && uv run --python 3.12 --extra dev coverage report -m --include='src/awf/control/executor/helpers.py' --fail-under=0`
    - Passed the targeted test run and confirmed the new tests exercise the
      intended helper branches without invoking the repository-wide coverage
      gate.

- Record broad validation ownership:
  Complete.
  - Full `python-full-coverage`, full unit suite, and `ci-required` are not run
    locally in this AWF agent phase. AWF/GitHub own those broad gates after
    agent completion.

## Gaps

No planned requirements remain open.
