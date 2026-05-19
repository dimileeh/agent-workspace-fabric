# PRRT_kwDOSJAM6s6DUWH3 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DUWH3_PLAN.md`

## Requirement Status

- Complete: Missing NUL delimiters from non-empty `-z` diff output are treated
  as malformed output.
- Complete: Truncated NUL-delimited records raise parse errors instead of
  returning partial paths.
- Complete: `_changed_paths_between_ref_and_head` converts parser failures into
  `ProtectedScopeDiffError` with protected-scope context.
- Complete: Valid NUL-delimited output still returns a deduplicated tuple.

## Evidence

- Updated `src/awf/runtime/pr_monitor_runner.py`.
- Updated regression coverage in
  `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`.
- Updated adjacent fake `--name-status -z` fixtures in
  `tests/unit/runtime/test_monitor_action_logging.py`.
- Added `plans/PRRT_kwDOSJAM6s6DUWH3_PLAN.md` and this validation file.

## Verification Commands

- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_action_logging.py -q`
- Passed: `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/runtime/test_monitor_action_logging.py`
- Passed: `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/runtime/test_monitor_action_logging.py`
- Passed: `uv run --python 3.12 --extra dev mypy src/awf`

No remaining gaps.
