# Pinned Workflow With Input Guard Validation

Plan reference: `plans/PINNED_WORKFLOW_WITH_INPUT_GUARD_PLAN.md`

## Requirement Status

- Complete: Added
  `test_workflow_pinned_uses_version_bump_blocks_sensitive_with_input`, covering
  both sensitive `token` inputs and an unsafe `secrets.*` expression during an
  allowed pinned `uses:` bump.
- Complete: Preserved
  `test_workflow_pinned_uses_version_bump_allows_with_input_update`, so safe
  non-sensitive action input updates remain allowed.
- Complete: Unsafe pinned-bump `with:` changes now report a dedicated
  `jobs.<job>.steps.<step>.with` violation.
- Complete: Updated `docs/PROTECTED_FILES.md` to document safe pinned-bump
  input updates and blocked sensitive/unsafe inputs.
- Complete: Kept all work on the current AWF branch with no push.

## Evidence

Changed files:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `docs/PROTECTED_FILES.md`
- `plans/PINNED_WORKFLOW_WITH_INPUT_GUARD_PLAN.md`
- `plans/PINNED_WORKFLOW_WITH_INPUT_GUARD_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_version_bump_blocks_sensitive_with_input tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_version_bump_allows_with_input_update -q
```

Initial result before implementation: the new regression failed because the
classifier returned zero violations. After implementation: `4 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q
```

Result: `286 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py
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

## Gaps

None.
