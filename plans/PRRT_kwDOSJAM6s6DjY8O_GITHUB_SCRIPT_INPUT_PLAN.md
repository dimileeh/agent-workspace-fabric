# PRRT_kwDOSJAM6s6DjY8O GitHub Script Input Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6DjY8O` reports that an added
`actions/github-script` comment/notify step is admitted when it has no `with`
block. Since `actions/github-script` only performs meaningful work through its
`script` input, admitting a no-input step is too permissive and can obscure the
script-safety requirement.

Scope is limited to the quality-gate helper for github-script comment/notify
steps and its unit coverage.

## Requirements Checklist

- Require `actions/github-script` comment/notify steps to include a safe
  `with.script` value before admission.
- Keep existing safe github-script comment scripts admitted.
- Keep unsafe github-script scripts or unsafe inputs blocked.
- Add/update a regression test for the no-`with` case.
- Commit only the files changed for this review thread.

## Implementation Steps

1. Update the existing permissive no-`with` github-script test to expect a
   quality-gate violation.
2. Run the targeted test first and confirm it fails against the current
   implementation.
3. Change `_github_script_comment_notify_inputs_are_safe` so `inputs is None`
   returns `False`.
4. Re-run targeted quality-gate tests covering github-script comment actions.
5. Create validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "github_script"`
  must pass after the implementation.
