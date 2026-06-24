# Orphan Reaping API Expectations Validation

Plan reference: `plans/ORPHAN_REAPING_API_EXPECTATIONS_PLAN.md`

## Requirement Status

- Verify source returns the reaping-enabled reason: Complete. The orphan summary
  branch in `src/awf/service/orphan_resources.py` selects
  `ORPHANS_PRESENT_REAPING_ENABLED` when `reaping_enabled` is true.
- Preserve cleanup readiness assertions: Complete. Existing assertions for
  `dry_run_only is False` and the reaping-enabled action text remain in the
  focused tests.
- Update focused stale expectations only: Complete. The reaping-enabled health
  and metrics tests now expect `ORPHANS_PRESENT_REAPING_ENABLED`; unrelated
  reaping-disabled assertions remain unchanged.
- Run targeted tests only: Complete. Broad AWF/GitHub validation, full coverage,
  and CI-equivalent commands were not run in the agent phase because AWF owns
  those after completion.
- Commit the scoped fix locally: Complete. The scoped files were committed for
  review thread `PRRT_kwDOSJAM6s6L7dMw`.

## Evidence

Pre-edit targeted command:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_orphan_resources_reflect_auto_cleanup_enabled tests/unit/api/test_metrics_capacity.py::test_default_orphan_resource_summary_propagates_auto_cleanup_setting -q
```

Result: failed on the two stale `ORPHAN_RESOURCES_PRESENT` expectations while
the implementation returned `ORPHANS_PRESENT_REAPING_ENABLED`.

Post-edit targeted command:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_orphan_resources_reflect_auto_cleanup_enabled tests/unit/api/test_metrics_capacity.py::test_default_orphan_resource_summary_propagates_auto_cleanup_setting -q
```

Result: `2 passed in 1.57s`.

## Files Changed

- `tests/unit/api/test_health_parts/test_health_part_002.py`
- `tests/unit/api/test_metrics_capacity.py`
- `plans/ORPHAN_REAPING_API_EXPECTATIONS_PLAN.md`
- `plans/ORPHAN_REAPING_API_EXPECTATIONS_VALIDATION.md`
