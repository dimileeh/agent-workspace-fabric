# Review 4491715538 Comment Action With Validation

Plan reference: `REVIEW_4491715538_COMMENT_ACTION_WITH_PLAN.md`

## Requirement Status

- Complete: Added regression coverage showing an added
  `peter-evans/create-or-update-comment@v4` step with `with.body` is allowed as
  informational.
- Complete: Allowed `with` through `_INFORMATIONAL_STEP_ALLOWED_KEYS` so the
  existing comment-action policy can evaluate the step.
- Complete: Preserved fail-closed behavior for `actions/github-script` steps
  with a `with` script; the focused regression command includes that blocked
  case.
- Complete: Verified the existing index-refspec deletion regression remains
  covered.
- Complete: Work remains on the current AWF-managed branch and has not been
  pushed.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/REVIEW_4491715538_COMMENT_ACTION_WITH_PLAN.md`
- `plans/REVIEW_4491715538_COMMENT_ACTION_WITH_VALIDATION.md`

Pre-fix failure:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "comment_action_step_with_body or github_script_step_with_script"`
  failed with `test_added_comment_action_step_with_body_is_allowed` producing an
  added-step protected workflow violation.

Passing verification:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "comment_action_step_with_body or github_script_step_with_script"`
  passed: 2 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_executor_coverage_edges.py -q -k "missing_index_path or staged_protected_file_diffs_treat_deleted_index_path_as_absent"`
  passed: 2 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py src/awf/control/protected_file_diffs.py tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_executor_coverage_edges.py`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passed: 215 passed.

## Gaps

None.
