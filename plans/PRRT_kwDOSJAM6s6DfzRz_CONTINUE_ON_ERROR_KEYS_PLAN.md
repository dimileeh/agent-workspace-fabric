# PRRT_kwDOSJAM6s6DfzRz Continue-On-Error Step Keys Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6DfzRz` reports that protected workflow edits can
enable `continue-on-error: true` on an existing comment/notify step without
requiring the resulting step to satisfy the informational-step key allowlist.
That can suppress failures for a comment-labeled step that still carries
dangerous unchanged fields such as `shell`.

Scope is limited to the protected workflow quality-gate classifier in
`src/awf/control/quality_gates.py` and focused regression coverage in
`tests/unit/control/test_quality_gates.py`.

## Requirements Checklist

- Add regression coverage proving `continue-on-error: true` is blocked when the
  target comment step has non-informational keys such as `shell`.
- Preserve existing allowance for safe comment/notify informational `run` and
  `uses` steps to opt into `continue-on-error: true`.
- Reuse the existing informational-step classifier rather than creating a
  parallel key policy.
- Keep the change narrow and fail closed for unsupported protected workflow
  shapes.

## Implementation Steps

1. Add a failing unit test for an unowned protected workflow diff that only
   enables `continue-on-error: true` on a comment-labeled step with a custom
   `shell`.
2. Update `_allows_comment_continue_on_error` so the exemption requires the
   resulting step to satisfy informational-step semantics, including the step key
   allowlist.
3. Run the focused failing regression, then the nearby continue-on-error
   regression group.
4. Create the required validation document with requirement status and command
   evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_comment_continue_on_error_with_custom_shell_is_blocked -q`
  passes after implementation and fails before it.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k 'continue_on_error and workflow_comment'`
  passes, proving existing safe comment continue-on-error behavior remains
  intact.
