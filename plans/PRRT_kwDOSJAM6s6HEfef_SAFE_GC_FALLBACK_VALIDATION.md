# PRRT_kwDOSJAM6s6HEfef Safe GC Fallback Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6HEfef_SAFE_GC_FALLBACK_PLAN.md`

## Requirement Status

- Complete: Missing-workspace fallback compose teardown behavior remains covered
  and passing.
- Complete: Existing `COMPLETED_PR_NOT_MERGED` preserved fallback behavior
  remains covered and passing.
- Complete: `WORKSPACE_CLEANUP_DISABLED` no longer fabricates a fallback compose
  teardown candidate, and active lease state is not revoked.
- Complete: `FAILED_WORKSPACE_TRIAGE_PRESERVED` no longer fabricates a fallback
  compose teardown candidate, and active lease state is not revoked.
- Complete: Focused regression tests were added for the unsafe preserved
  reasons.
- Complete: Only targeted checks were run; full AWF/GitHub validation remains
  managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/service/gc.py`
- `tests/unit/service/test_gc_parts/test_gc_part_001.py`
- `plans/PRRT_kwDOSJAM6s6HEfef_SAFE_GC_FALLBACK_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6HEfef_SAFE_GC_FALLBACK_VALIDATION.md`

Focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_001.py -q -k "single_workspace_gc_tears_down_compose_for_preserved_workspace or single_workspace_fallback_compose_teardown_releases_runtime_side_effects or cleanup_disabled_skips_fallback_compose_teardown or triage_preserved_skips_fallback_compose_teardown"
```

Result before implementation: failed because the fallback callback received
`WORKSPACE_CLEANUP_DISABLED` and `FAILED_WORKSPACE_TRIAGE_PRESERVED`.

Result after implementation: `4 passed, 29 deselected`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_002.py::test_single_workspace_gc_reports_failed_missing_workspace_compose_teardown -q
```

Result: `1 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/service/gc.py tests/unit/service/test_gc_parts/test_gc_part_001.py
```

Result: passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf/service/gc.py
```

Result: passed.

Full AWF/GitHub validation, whole-repository tests, and coverage gates were not
run in the agent phase per the workspace contract.

## Remaining Gaps

None.
