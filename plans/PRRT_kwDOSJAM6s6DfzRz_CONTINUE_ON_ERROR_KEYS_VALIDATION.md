# PRRT_kwDOSJAM6s6DfzRz Continue-On-Error Step Keys Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DfzRz_CONTINUE_ON_ERROR_KEYS_PLAN.md`

## Requirement Status

- Add regression coverage proving `continue-on-error: true` is blocked when the
  target comment step has non-informational keys such as `shell`: Complete.
  - Evidence: added
    `test_workflow_comment_continue_on_error_with_custom_shell_is_blocked` in
    `tests/unit/control/test_quality_gates.py`.
- Preserve existing allowance for safe comment/notify informational `run` and
  `uses` steps to opt into `continue-on-error: true`: Complete.
  - Evidence: the existing workflow comment continue-on-error regression group
    passed.
- Reuse the existing informational-step classifier rather than creating a
  parallel key policy: Complete.
  - Evidence: `_allows_comment_continue_on_error` now delegates to
    `_is_informational_step` after confirming the step is comment/notify
    labeled.
- Keep the change narrow and fail closed for unsupported protected workflow
  shapes: Complete.
  - Evidence: implementation is limited to
    `src/awf/control/quality_gates.py` and one focused regression test.

## Command Evidence

- Initial red regression before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_comment_continue_on_error_with_custom_shell_is_blocked -q`
  failed with `assert 0 == 1`.
- Focused regression after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_comment_continue_on_error_with_custom_shell_is_blocked -q`
  passed.
- Nearby behavior regression group:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k 'continue_on_error and workflow_comment'`
  passed with 8 passed.
- Full quality-gate unit file:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passed with 289 passed.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passed.

## Gaps

None.
