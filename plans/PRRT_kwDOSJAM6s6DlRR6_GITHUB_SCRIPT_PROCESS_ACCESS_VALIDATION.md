# PRRT_kwDOSJAM6s6DlRR6 GitHub Script Process Access Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DlRR6_GITHUB_SCRIPT_PROCESS_ACCESS_PLAN.md`

## Requirement Status

- Complete: Added a regression test showing a comment-labeled
  `actions/github-script` step that reads `process['env']` is blocked.
  Evidence: `tests/unit/control/test_quality_gates.py`.
- Complete: Kept existing safe GitHub comment scripts admitted.
  Evidence: focused `github_script` test selection passed after the fix.
- Complete: Kept existing fail-closed behavior for other unsafe script access.
  Evidence: existing unsafe input parametrizations still pass after adding the
  bracketed `process` case.
- Complete: Kept the change scoped to this review thread.
  Evidence: changed only the quality-gate regex, focused unit test coverage,
  and this plan/validation pair.

## Verification Evidence

- Before production fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "github_script"`
  failed on the new `process['env']` regression because zero violations were
  reported.
- After production fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "github_script"`
  passed: `13 passed, 320 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py`
  passed.

## Gaps

None.
