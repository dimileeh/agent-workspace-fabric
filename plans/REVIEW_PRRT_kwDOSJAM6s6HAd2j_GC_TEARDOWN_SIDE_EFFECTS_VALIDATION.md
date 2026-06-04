# Review Thread PRRT_kwDOSJAM6s6HAd2j: GC Teardown Side Effects Validation

## Plan Validation

- Verified the review claim against `src/awf/runtime/pr_monitor_runner/lifecycle.py`
  and `src/awf/service/gc.py`.
- Confirmed `run_workspace_filesystem_gc` previously revoked secret leases before
  compose teardown and released reservations for all candidates after path
  deletion, even when compose teardown skipped those paths.
- Added focused regressions:
  - Monitor completion compose teardown failure preserves active secret leases.
  - GC compose teardown failure preserves active resource reservations.
- Updated GC execution so compose teardown results are computed first, and lease
  revocation/resource release are limited to candidates whose compose teardown is
  absent, skipped, or succeeded.
- Reused precomputed teardown results during path deletion so callbacks are not
  invoked twice and failed teardowns still produce skipped path outcomes.

## Focused Evidence

Initial regression check before production fix:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_skips_filesystem_gc_when_compose_teardown_fails -q
```

Result: failed as expected because the lease status was `revoked` after compose
teardown failure.

Focused checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_skips_filesystem_gc_when_compose_teardown_fails tests/unit/service/test_gc_more2.py::test_gc_compose_teardown_failure_preserves_resource_reservations -q
```

Result: `2 passed`.

Adjacent compose-teardown/retry checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_skips_filesystem_gc_when_compose_teardown_fails tests/unit/service/test_gc_more2.py::test_gc_compose_teardown_failure_preserves_resource_reservations tests/unit/service/test_gc_parts/test_gc_part_002.py::test_single_workspace_cleanup_is_idempotent_after_partial_compose_failure tests/unit/service/test_gc_parts/test_gc_part_002.py::test_gc_accepts_sync_compose_teardown_result tests/unit/service/test_gc_parts/test_gc_part_003.py::test_cleanup_is_idempotent_after_partial_compose_failure tests/unit/service/test_gc_parts/test_gc_part_003.py::test_service_gc_unmounts_auth_overlay_after_compose_teardown tests/unit/service/test_gc_parts/test_gc_part_003.py::test_service_gc_skips_overlay_unmount_when_compose_teardown_fails tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_passes_volume_reaping_compose_teardown_to_filesystem_gc -q
```

Result: `8 passed`.

Focused static checks:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/service/gc.py tests/unit/runtime/test_monitor_completion_gc.py tests/unit/service/test_gc_more2.py
uv run --python 3.12 --extra dev mypy src/awf/service/gc.py
```

Result: ruff passed; mypy passed.

## Broad Validation

Not run in this agent phase. Per AWF workspace contract, broad repository
validation, full coverage gates, and CI-equivalent checks are owned by AWF/GitHub
after agent completion.
