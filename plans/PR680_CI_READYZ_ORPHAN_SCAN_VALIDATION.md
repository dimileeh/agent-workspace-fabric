# PR680 CI Readiness Orphan Scan Fix Validation

Plan reference: `plans/PR680_CI_READYZ_ORPHAN_SCAN_PLAN.md`

## Requirement status

- Complete: Keep `/readyz` orphan-resource behavior expectations intact.
  - The two manual-cleanup tests now explicitly disable `auto_cleanup_orphans`
    and still assert `503`, `ORPHAN_RESOURCES_PRESENT`, orphan counts, examples,
    and blocked dry-run cleanup readiness.
  - The reaping-enabled test continues to assert
    `ORPHANS_PRESENT_REAPING_ENABLED` and `dry_run_only is False`.
- Complete: Replace order-sensitive queued Docker output in affected tests with
  a command-aware fake runner.
  - `tests/unit/api/test_health_parts/test_health_part_002.py` now uses
    `_ReadyzDockerRunner` for orphan-resource inventory scenarios so concurrent
    `/readyz` checks receive output by Docker argv pattern instead of FIFO order.
- Complete: Preserve existing assertions for reason codes, counts, examples,
  and cleanup readiness.
  - Existing orphan-resource assertions remain in place with test names updated
    to document manual cleanup mode.
- Complete: Run focused local repro commands only.
  - Full AWF/GitHub CI and aggregate coverage validation were not run locally;
    AWF/GitHub own that broad validation after agent completion.
- Complete: Commit the scoped fix locally.
  - Commit will be created after this validation document is saved.

## Evidence

Files changed:

- `tests/unit/api/test_health_parts/test_health_part_002.py`
- `plans/PR680_CI_READYZ_ORPHAN_SCAN_PLAN.md`
- `plans/PR680_CI_READYZ_ORPHAN_SCAN_VALIDATION.md`

Commands run:

- Failing repro before fix:
  - `uv run --python 3.12 pytest tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_terminal_workspace_with_live_container_reports_leak tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_orphan_resources_present_returns_503 -q`
  - Result: failed locally with `200 == 503`, matching CI.
- Focused repro after fix:
  - `uv run --python 3.12 pytest tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_terminal_workspace_with_live_container_reports_leak_without_auto_cleanup tests/unit/api/test_health_parts/test_health_part_002.py::test_readyz_orphan_resources_present_without_auto_cleanup_returns_503 -q`
  - Result: `2 passed`.
- Focused containing test file:
  - `uv run --python 3.12 pytest tests/unit/api/test_health_parts/test_health_part_002.py -q`
  - Result: `11 passed`.
- Narrow lint:
  - `uv run --python 3.12 ruff check tests/unit/api/test_health_parts/test_health_part_002.py`
  - Result: `All checks passed!`

## Remaining gaps

None for the scoped fix. Full CI, including `python-coverage-shards`,
`python-full-coverage`, and `ci-required`, remains delegated to AWF/GitHub after
agent completion per the workspace contract.
