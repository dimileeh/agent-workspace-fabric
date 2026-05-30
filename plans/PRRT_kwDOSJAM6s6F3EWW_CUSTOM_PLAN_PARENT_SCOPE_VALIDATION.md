# PRRT_kwDOSJAM6s6F3EWW Custom Plan Parent Scope Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F3EWW_CUSTOM_PLAN_PARENT_SCOPE_PLAN.md`

## Requirement Status

- Preserve real repository directory scopes such as `docs/runbooks/**` when a
  custom planning artifact is only a workspace-id filename in that directory:
  Complete.
- Continue filtering exact generated custom artifact filenames and workspace-id
  filename globs: Complete.
- Continue supporting directory-scope artifact filtering when the template's
  parent directory itself is workspace-specific: Complete.
- Keep the default `docs/awf-plans/**` internal artifact behavior unchanged:
  Complete.
- Run only focused validation for the changed owned-path helper: Complete.

## Evidence

Changed files:

- `src/awf/common/owned_paths.py`
- `tests/unit/common/test_owned_paths.py`
- `plans/PRRT_kwDOSJAM6s6F3EWW_CUSTOM_PLAN_PARENT_SCOPE_PLAN.md`

Focused validation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q
```

Result: `26 passed in 0.43s`.

Full AWF/GitHub validation is managed by AWF after agent completion and was not
run during this focused review-thread fix cycle.
