# Pinned Workflow With Input Guard Plan

## Problem Statement And Scope

PR thread `PRRT_kwDOSJAM6s6DfCz9` reports that allowed pinned workflow `uses:`
bumps ignore `with:` input changes entirely. Existing protected-gate policy
allows safe action input updates during pinned bumps, so the scope is to add a
security guard for unsafe/sensitive `with:` changes without breaking that
allowed path.

## Requirements Checklist

- Add a regression proving a pinned action bump with `with: token:
  ${{ secrets.* }}` is blocked.
- Preserve the existing allowed safe `with:` update case for pinned version
  bumps.
- Report unsafe `with:` changes with a dedicated step `.with` violation instead
  of a generic remainder violation.
- Keep the change local to protected quality-gate workflow classification and
  operator documentation.
- Commit the fix locally without switching branches or pushing.

## Implementation Steps

1. Add the focused failing regression in
   `tests/unit/control/test_quality_gates.py`.
2. Run the new test and confirm it fails against the current classifier.
3. Add a pinned-bump `with:` input safety check in
   `src/awf/control/quality_gates.py`.
4. Update `docs/PROTECTED_FILES.md` to document safe `with:` updates and the
   sensitive/unsafe expression block.
5. Run focused tests, ruff on touched files, and mypy for `src/awf`.
6. Record validation in
   `plans/PINNED_WORKFLOW_WITH_INPUT_GUARD_VALIDATION.md`.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_version_bump_blocks_sensitive_with_input tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_version_bump_allows_with_input_update -q
uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py
uv run --python 3.12 --extra dev mypy src/awf
```

Pass criteria: the new regression fails before implementation and all listed
commands pass after implementation.
