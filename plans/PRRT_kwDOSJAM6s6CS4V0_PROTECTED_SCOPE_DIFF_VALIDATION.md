# PRRT_kwDOSJAM6s6CS4V0 Protected Scope Diff Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6CS4V0_PROTECTED_SCOPE_DIFF_PLAN.md`

## Requirement Status

- Add a regression test proving a protected-scope repair baseline failure is
  surfaced as `PROTECTED_SCOPE_DIFF_UNAVAILABLE`: Complete.
- Preserve fail-closed behavior: do not stage, commit, or push after the
  baseline verification fails: Complete.
- Keep successful protected-scope restore filtering behavior unchanged:
  Complete.
- Keep existing push-time protected-scope diff handling unchanged: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`

Plan and validation files:

- `plans/PRRT_kwDOSJAM6s6CS4V0_PROTECTED_SCOPE_DIFF_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CS4V0_PROTECTED_SCOPE_DIFF_VALIDATION.md`

Commands run:

- Initial red test:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q -k "protected_revert_diff_baseline_unavailable or protected_revert_check_errors"`
  failed before the runtime change.
- Focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q -k "protected_revert_diff_baseline_unavailable or protected_revert_check_errors"`
  passed.
- Broader protected-scope/runtime edge suite:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
  passed, 129 tests.
- Companion PR monitor unit suite:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py -q`
  passed, 107 tests.
- Static checks:
  `uv run --python 3.12 --extra dev ruff check src/awf tests`
  passed.
- Type checks:
  `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Gaps

No remaining gaps.
