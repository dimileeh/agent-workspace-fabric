# PRRT_kwDOSJAM6s6De8qm Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6De8qm_PLAN.md`

## Requirement Status

- Complete: Preserved active branch PR recovery now searches open PRs by the
  recovered head branch without filtering by `workspace.branch_base`.
  - Evidence: `src/awf/control/worker.py` passes `base_branch=None` when
    recovering a preserved active branch PR.
- Complete: Matched, failed, ambiguous, and fallback-to-branch-name recovery
  behavior remains covered.
  - Evidence: the focused `preserved_active_pushed_branch` worker tests pass.
- Complete: Existing head repository mismatch and multiple-match ambiguity
  protections remain in place.
  - Evidence: `_resolve_preserved_active_branch_open_pr()` still filters by the
    expected head repository slug and returns ambiguity for invalid or multiple
    resolver matches.
- Complete: Regression coverage was updated before the production change.
  - Evidence: the targeted retargeted-PR test failed before implementation
    because the monitor recovery task never started when the resolver returned
    no match for a base-filtered lookup.
- Complete: Files changed are scoped to this thread.
  - Evidence: touched files are the worker recovery call site, focused worker
    tests, and this thread's plan/validation docs.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "preserved_active_pushed_branch_open_pr_attaches_one_monitor" -q`
  - Pre-fix result: failed for `running` and `pushing`, proving the regression.
  - Post-fix result: `2 passed, 213 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "preserved_active_pushed_branch" -q`
  - Result: `8 passed, 207 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: `All checks passed!`
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  - Result: `Success: no issues found in 1 source file`

## Remaining Gaps

None.
