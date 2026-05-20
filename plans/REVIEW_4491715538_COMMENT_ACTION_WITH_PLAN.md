# Review 4491715538 Comment Action With Plan

## Problem Statement and Scope

Address the remaining actionable portion of review-level comment
`issue:4491715538`: added comment-action workflow steps using
`peter-evans/create-or-update-comment` are intended to be classified as safe
informational steps, but the informational step key allowlist rejects `with`
before the action allowlist is evaluated.

The staged protected-file deletion report in the same review comment is already
addressed in this workspace by the existing index-refspec deletion fix and
regression coverage, so this plan is scoped to the workflow comment-action
`with:` path.

## Requirements Checklist

- Add regression coverage showing an added comment-action step with `with.body`
  is allowed as informational.
- Allow `with` through the initial informational-step key allowlist so the
  existing comment-action policy can evaluate it.
- Preserve the existing fail-closed behavior for `actions/github-script` steps
  that include a `with` script.
- Verify the existing index-refspec deletion regression remains covered.
- Commit the scoped fix locally on the current AWF-managed branch without
  pushing or switching branches.

## Implementation Steps

1. Add a failing unit test in `tests/unit/control/test_quality_gates.py` for an
   added `peter-evans/create-or-update-comment@v4` step with `with.body`.
2. Run the focused test and confirm it fails before the classifier change.
3. Add `with` to `_INFORMATIONAL_STEP_ALLOWED_KEYS` in
   `src/awf/control/quality_gates.py`.
4. Run focused quality-gate tests, the existing index-refspec deletion tests,
   and static checks for the touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "comment_action_step_with_body or github_script_step_with_script"` fails before the fix for the new regression and passes after the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_executor_coverage_edges.py -q -k "missing_index_path or staged_protected_file_diffs_treat_deleted_index_path_as_absent"` passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py src/awf/control/protected_file_diffs.py tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_executor_coverage_edges.py` passes.
