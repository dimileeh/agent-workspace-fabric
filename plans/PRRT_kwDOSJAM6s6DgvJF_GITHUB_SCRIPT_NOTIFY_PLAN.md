# PRRT_kwDOSJAM6s6DgvJF GitHub Script Notify Plan

## Problem Statement and Scope

The protected workflow informational-step classifier lists
`actions/github-script` as comment/notify capable, but functional
`actions/github-script` usage requires `with.script`. The current guard rejects
all `with:` blocks for that action, so a real PR-comment script is blocked even
when the step is labeled as comment/notify.

Scope is limited to the workflow comment/notify action classifier in
`src/awf/control/quality_gates.py`, focused unit coverage in
`tests/unit/control/test_quality_gates.py`, and this plan/validation pair.

## Requirements Checklist

- Add regression coverage showing a comment-labeled
  `actions/github-script@v7` step with a PR-comment `with.script` is allowed.
- Preserve existing blocking coverage for arbitrary `actions/github-script`
  scripts that execute validation commands.
- Keep `actions/github-script` input handling fail-closed for non-mapping,
  unknown, or unsafe inputs.
- Reuse the existing unsafe GitHub Actions expression guard for script text.
- Commit the scoped fix locally without pushing or switching branches.

## Implementation Steps

1. Add a failing unit test for a real `github-script` PR comment script.
2. Run the focused test and confirm the new regression fails before the
   production change.
3. Add constrained `github-script` input/script validation that permits only
   comment API scripts and blocks shell/process/import/network escape hatches.
4. Run focused quality-gate unit tests and style/type checks for the touched
   Python files.
5. Record validation evidence in
   `plans/PRRT_kwDOSJAM6s6DgvJF_GITHUB_SCRIPT_NOTIFY_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "github_script_step_with_comment_script or github_script_step_with_script or workflow_comment_named_github_script_continue_on_error_with_script"` fails before the fix for the new allowed regression and passes after the fix.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py` passes.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py` passes.
