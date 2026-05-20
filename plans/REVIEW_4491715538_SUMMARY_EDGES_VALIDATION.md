# Review 4491715538 Summary Edges Validation

Plan reference: `plans/REVIEW_4491715538_SUMMARY_EDGES_PLAN.md`

## Requirement Status

- Complete: Preserved the existing regression that a `fail_under` threshold
  change plus another `tool.coverage` change reports both violations.
- Complete: Added specific diagnostics for added and removed
  `tool.coverage.report.fail_under` values, avoiding "must remain numeric"
  when the value was not previously present or was removed.
- Complete: Kept reporting other `tool.coverage` policy changes when an added,
  removed, or type-changed `fail_under` appears in the same diff.
- Complete: Made broad validation command detection quote-aware before command
  segment splitting.
- Complete: Preserved detection of real broad validation commands after shell
  separators and newlines.
- Complete: Expanded pinned action `with:` value-update allowlisting beyond
  `actions/setup-python` by allowing already-present safe `actions/cache`
  cache-key/path input updates during pinned version bumps.
- Complete: Updated `docs/PROTECTED_FILES.md` to document the expanded
  pinned-bump input allowlist.
- Complete: Kept the work on the current AWF-managed branch with no push or
  branch switch.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `docs/PROTECTED_FILES.md`
- `plans/REVIEW_4491715538_SUMMARY_EDGES_PLAN.md`
- `plans/REVIEW_4491715538_SUMMARY_EDGES_VALIDATION.md`

Initial focused regression run before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_pyproject_added_coverage_fail_under_message_is_specific tests/unit/control/test_quality_gates.py::test_pyproject_removed_coverage_fail_under_message_is_specific tests/unit/control/test_quality_gates.py::test_broad_validation_command_detection_covers_wrappers_and_deploy_tools tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_version_bump_allows_cache_input_update -q
```

Result: failed with four expected failures covering added/removed
`fail_under` wording, quoted `&&` broad command splitting, and `actions/cache`
pinned-bump input updates.

Verification after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_pyproject_added_coverage_fail_under_message_is_specific tests/unit/control/test_quality_gates.py::test_pyproject_removed_coverage_fail_under_message_is_specific tests/unit/control/test_quality_gates.py::test_broad_validation_command_detection_covers_wrappers_and_deploy_tools tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_version_bump_allows_cache_input_update -q
```

Result: `17 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q
```

Result: `321 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py docs/PROTECTED_FILES.md
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --extra dev ruff format --check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py
```

Result: `2 files already formatted`.

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: `Success: no issues found in 158 source files`.

## Review Feedback Disposition

No gaps remain for the planned requirements. The review comment also described
new optional `with:` input additions during pinned bumps; those remain
intentionally blocked because existing pinned-input guardrail documentation and
regressions treat arbitrary add/remove input changes as unsafe unless AWF can
prove the action/input pair is safe locally.
