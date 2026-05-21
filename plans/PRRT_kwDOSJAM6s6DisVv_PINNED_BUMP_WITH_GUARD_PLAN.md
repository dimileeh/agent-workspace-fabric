# PRRT_kwDOSJAM6s6DisVv Pinned Bump With Guard Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6DisVv` reports that an unowned protected
workflow edit can bundle arbitrary `with:` input changes with an otherwise
allowed pinned `uses:` version bump. The current safety check only rejects
sensitive-looking input names and unsafe GitHub Actions expressions, so
behavioral inputs such as `actions/github-script` `script` can be rewritten
while the remainder comparison ignores `with:`.

Scope is limited to protected GitHub workflow classification, focused
regression coverage, and operator documentation.

## Requirements Checklist

- Add a regression proving a pinned `actions/github-script` bump cannot rewrite
  `with.script` in an unowned protected workflow.
- Preserve the existing regression that permits the documented safe
  `actions/setup-python` `python-version` update during a pinned bump.
- Replace the broad non-sensitive-input allowance with an action-specific
  allowlist for pinned-bump `with:` edits.
- Keep unsafe pinned-bump `with:` changes reported as a dedicated `.with`
  violation.
- Update protected-file documentation to describe the narrowed allowance.
- Commit the fix locally without switching branches or pushing.

## Implementation Steps

1. Add the focused failing regression in
   `tests/unit/control/test_quality_gates.py`.
2. Run the new regression to confirm the current classifier allows the unsafe
   script rewrite.
3. Update `src/awf/control/quality_gates.py` so pinned-bump `with:` edits must
   be both non-sensitive and explicitly allowed for the action/input key.
4. Update `docs/PROTECTED_FILES.md` to document approved input updates and
   blocked arbitrary or executable input edits.
5. Run focused quality-gate tests plus lint/type checks for the touched surface.
6. Record validation in
   `plans/PRRT_kwDOSJAM6s6DisVv_PINNED_BUMP_WITH_GUARD_VALIDATION.md`.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_version_bump_blocks_github_script_input_rewrite -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_version_bump_blocks_github_script_input_rewrite tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_version_bump_blocks_sensitive_with_input tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_version_bump_allows_with_input_update -q
uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py docs/PROTECTED_FILES.md
uv run --python 3.12 --extra dev mypy src/awf
```

Pass criteria: the new regression fails before the implementation and all
listed commands pass after the implementation.
