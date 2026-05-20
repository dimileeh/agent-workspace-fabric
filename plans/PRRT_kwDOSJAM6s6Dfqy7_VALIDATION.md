# PRRT_kwDOSJAM6s6Dfqy7 Validation

Plan reference: `PRRT_kwDOSJAM6s6Dfqy7_PLAN.md`

## Requirement Status

- Complete: Do not transition preserved-active salvage to `monitoring_pr`
  unless a PR number is available.
- Complete: Recover legacy rows with a parseable GitHub PR URL by deriving and
  persisting `pr_number` before monitor attachment.
- Complete: Leave branch-based open PR lookup behavior intact when no
  attachable PR URL exists.
- Complete: Add regression coverage that fails against the prior
  implementation.
- Complete: Run the narrowest relevant test proving the fix.

## Evidence

- Changed `src/awf/control/worker.py` so preserved-active PR monitor recovery
  derives `pr_number` from parseable PR URLs and guards monitor attachment when
  it remains unavailable.
- Changed `tests/unit/control/test_worker.py` with regressions for parseable
  missing PR numbers and unparseable PR URLs.
- Confirmed the new focused regressions failed before the implementation.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_pr_handoff'`
  passed with 4 passed and 215 deselected.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passed.
