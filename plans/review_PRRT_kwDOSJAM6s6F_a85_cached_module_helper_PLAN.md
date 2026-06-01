# Review PRRT_kwDOSJAM6s6F-a85 Cached Module Helper Plan

## Problem Statement and Scope

The review thread reports that `_clear_cached_module` is duplicated in three unit test files:

- `tests/unit/service/test_metrics_import_order.py`
- `tests/unit/service/test_workspace_retry_import_cycle.py`
- `tests/unit/common/test_github_client_split_imports.py`

The scope is limited to extracting that duplicated test helper into a shared unit-test utility and updating those tests to use it.

## Requirements Checklist

- [ ] Preserve existing import-order regression behavior.
- [ ] Remove the duplicated helper implementation from the three test files.
- [ ] Keep the shared helper lightweight so it does not import AWF production modules that could mask import-order regressions.
- [ ] Run focused verification for the three changed tests only.
- [ ] Do not run broad AWF/GitHub-owned validation in the agent phase.

## Implementation Steps

1. Add a lightweight shared helper for clearing cached modules under `tests/unit/`.
2. Replace the three local `_clear_cached_module` definitions with imports from the shared helper.
3. Remove imports that become unused after the extraction.
4. Run the focused pytest command for the three changed test files.

## Verification Commands and Pass Criteria

Focused verification:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics_import_order.py tests/unit/service/test_workspace_retry_import_cycle.py tests/unit/common/test_github_client_split_imports.py -q
```

Pass criteria: all selected tests pass. Full AWF/GitHub validation remains managed by AWF after agent completion.
