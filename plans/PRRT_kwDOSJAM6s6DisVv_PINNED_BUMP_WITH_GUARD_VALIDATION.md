# PRRT_kwDOSJAM6s6DisVv Pinned Bump With Guard Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DisVv_PINNED_BUMP_WITH_GUARD_PLAN.md`

## Requirement Status

- Complete: Added
  `test_workflow_pinned_uses_version_bump_blocks_github_script_input_rewrite`,
  proving a pinned `actions/github-script` bump cannot rewrite `with.script`
  in an unowned protected workflow.
- Complete: Preserved
  `test_workflow_pinned_uses_version_bump_allows_with_input_update`, so the
  documented `actions/setup-python` `python-version` update remains allowed.
- Complete: Replaced the broad non-sensitive-input allowance with an
  action-specific pinned-bump `with:` edit allowlist. The classifier now blocks
  unapproved keys and `with:` add/remove changes during pinned bumps.
- Complete: Unsafe pinned-bump `with:` changes still report the dedicated
  `jobs.<job>.steps.<step>.with` violation.
- Complete: Updated `docs/PROTECTED_FILES.md` to document the narrowed
  allowance and blocked arbitrary input edits.
- Complete: Kept the work on the current AWF branch with no push.

## Evidence

Changed files:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `docs/PROTECTED_FILES.md`
- `plans/PRRT_kwDOSJAM6s6DisVv_PINNED_BUMP_WITH_GUARD_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DisVv_PINNED_BUMP_WITH_GUARD_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_version_bump_blocks_github_script_input_rewrite -q
```

Initial result before implementation: failed because the classifier returned
zero violations for the bundled `actions/github-script` `script` rewrite.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_version_bump_blocks_github_script_input_rewrite tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_version_bump_blocks_sensitive_with_input tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_version_bump_allows_with_input_update -q
```

Result after implementation: `5 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q
```

Result: `310 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py docs/PROTECTED_FILES.md
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: `Success: no issues found in 158 source files`.

## Gaps

None.
