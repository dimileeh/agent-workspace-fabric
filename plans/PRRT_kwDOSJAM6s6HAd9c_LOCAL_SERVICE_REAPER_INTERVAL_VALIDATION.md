# Local Service Reaper Interval Pass-Through Validation

Plan: `plans/PRRT_kwDOSJAM6s6HAd9c_LOCAL_SERVICE_REAPER_INTERVAL_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| `docker/compose/local-service.yml` forwards `AWF_CLASSIFIED_ORPHAN_REAP_SCAN_INTERVAL_SECONDS` with a `3600` default. | Complete | Added the key beside the existing orphan cleanup environment settings. |
| `tests/integration/test_local_service_compose.py` asserts the environment key is present for both `api` and `worker`. | Complete | Extended `test_local_service_compose_declares_control_plane_stack`. |
| Focused test fails before the compose change and passes after it. | Complete | First run failed with `KeyError: 'AWF_CLASSIFIED_ORPHAN_REAP_SCAN_INTERVAL_SECONDS'`; second run passed. |
| Local validation is limited to the focused compose contract test. | Complete | Ran only the focused test listed below. Full AWF/GitHub validation and coverage remain owned by AWF after agent completion. |

## Commands

```bash
uv run --python 3.12 --extra dev pytest tests/integration/test_local_service_compose.py::test_local_service_compose_declares_control_plane_stack -q
```

Result: `1 passed in 0.40s`.

## Gaps

None.
