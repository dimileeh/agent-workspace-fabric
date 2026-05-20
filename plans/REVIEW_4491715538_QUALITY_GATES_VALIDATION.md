# Review 4491715538 Quality Gates Validation

Plan reference: `REVIEW_4491715538_QUALITY_GATES_PLAN.md`

## Requirement Status

- Complete: Allow pinned `actions/github-script` comment/notify steps that
  provide a safe `with.script` input, including when `continue-on-error: true`
  is added.
- Complete: Reject validation run replacements where the new command only has
  the old command as a string prefix rather than a shell-word boundary.
- Complete: Treat shell line continuations in multi-line informational `run:`
  commands as one logical shell command when evaluating safe comment/notify
  commands.
- Complete: Preserve existing protections for unsafe `github-script` inputs,
  validation removal/narrowing, unsafe shell operators, and secret
  interpolation.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/REVIEW_4491715538_QUALITY_GATES_PLAN.md`
- `plans/REVIEW_4491715538_QUALITY_GATES_VALIDATION.md`

Tests and checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "github_script_continue_on_error_with_safe_script or informational_run_command_shell_safety_edges or validation_run_preservation_allows_only_safe_validation_appends"`:
  passed, 56 passed and 251 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`:
  passed, 307 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`:
  passed.

## Notes

The `pytest` to `pytest-randomly` example was already rejected by the existing
append parser because safe preservation requires the suffix to start with `&&`.
The new regression locks that behavior so the plain string-prefix concern cannot
silently regress.
