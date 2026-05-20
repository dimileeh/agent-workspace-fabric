# PRRT_kwDOSJAM6s6DgvJF GitHub Script Notify Validation

Plan reference: `PRRT_kwDOSJAM6s6DgvJF_GITHUB_SCRIPT_NOTIFY_PLAN.md`

## Requirement Status

- Complete: Added regression coverage showing a comment-labeled
  `actions/github-script@v7` step with a PR-comment `with.script` is allowed.
- Complete: Preserved blocking coverage for arbitrary `actions/github-script`
  scripts that execute validation commands.
- Complete: Kept `actions/github-script` input handling fail-closed for
  unknown inputs, non-comment GitHub REST calls, and network escape hatches.
- Complete: Reused the existing unsafe GitHub Actions expression guard for
  script text.
- Complete: Work remains local on the current AWF-managed branch and has not
  been pushed.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/PRRT_kwDOSJAM6s6DgvJF_GITHUB_SCRIPT_NOTIFY_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DgvJF_GITHUB_SCRIPT_NOTIFY_VALIDATION.md`

Pre-fix failure:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "github_script_step_with_comment_script or github_script_step_with_script or workflow_comment_named_github_script_continue_on_error_with_script"`
  failed with `test_added_github_script_step_with_comment_script_is_allowed`
  producing an added-step protected workflow violation.

Passing verification:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "github_script_step_with_comment_script or github_script_step_with_script or workflow_comment_named_github_script_continue_on_error_with_script"`
  passed: 6 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passed: 302 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

Additional broad validation attempt:

- `uv run --python 3.12 --extra dev pytest tests/unit -q` was interrupted
  after 8 minutes 50 seconds because it was still early in the suite; at
  interruption it had 633 passed and no failures reported.

## Gaps

None for the planned review-thread scope.
