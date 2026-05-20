# PRRT_kwDOSJAM6s6DjY8O GitHub Script Input Validation

Plan reference: `PRRT_kwDOSJAM6s6DjY8O_GITHUB_SCRIPT_INPUT_PLAN.md`

## Requirement Status

- Require `actions/github-script` comment/notify steps to include a safe
  `with.script` value before admission: Complete.
- Keep existing safe github-script comment scripts admitted: Complete.
- Keep unsafe github-script scripts or unsafe inputs blocked: Complete.
- Add/update a regression test for the no-`with` case: Complete.
- Commit only the files changed for this review thread: Complete.

## Evidence

Changed files:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/PRRT_kwDOSJAM6s6DjY8O_GITHUB_SCRIPT_INPUT_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DjY8O_GITHUB_SCRIPT_INPUT_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "github_script_comment_step_without_script"`
  failed before implementation with `len(violations) == 0`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "github_script"`
  passed after implementation: 11 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passed.

## Gaps

No remaining gaps.
