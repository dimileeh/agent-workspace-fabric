# Review 4491715538 GitHub Script Input Keys Validation

Plan reference: `plans/REVIEW_4491715538_GITHUB_SCRIPT_INPUT_KEYS_PLAN.md`

## Requirement Status

- Complete: Pinned `actions/github-script` comment/notify steps now allow a
  safe comment script with supported non-security inputs: `debug`,
  `result-encoding`, `retries`, and `retry-exempt-status-codes`. Evidence:
  `test_added_github_script_step_with_comment_script_and_safe_options_is_allowed`.
- Complete: Unknown `actions/github-script` `with:` keys remain blocked.
  Existing evidence: `test_added_github_script_step_with_script_unsafe_inputs_are_blocked`
  includes the unsupported `github-token` input.
- Complete: Unsafe scripts and unsafe GitHub Actions expressions remain
  blocked. Existing evidence: the focused `github_script_step` test subset.
- Complete: SHA-to-SHA pinned action behavior and operator documentation were
  preserved. Existing evidence: `docs/PROTECTED_FILES.md` documents that
  switching between raw SHAs requires ownership because AWF cannot prove a
  local non-downgrade.
- Complete: Coverage-policy behavior for combined `fail_under` and other
  coverage setting changes was preserved. Existing evidence:
  `test_pyproject_fail_under_change_reports_other_coverage_policy_changes`.

## Verification Evidence

Initial regression run before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_github_script_step_with_comment_script_and_safe_options_is_allowed -q
```

Result: failed with one violation for the added `Post PR comment` step.

After implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_github_script_step_with_comment_script_and_safe_options_is_allowed -q
```

Result: `1 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "github_script_step"
```

Result: `7 passed, 302 deselected`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q
```

Result: `309 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: `Success: no issues found in 158 source files`.

## Gaps

None.
