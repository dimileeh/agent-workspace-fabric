# Local Service Reaper Interval Pass-Through Plan

Thread: `PRRT_kwDOSJAM6s6HAd9c`

## Problem Statement

`Settings` defines `classified_orphan_reap_scan_interval_seconds`, but the local
service Docker Compose environment does not forward
`AWF_CLASSIFIED_ORPHAN_REAP_SCAN_INTERVAL_SECONDS` into the `api` and `worker`
containers. Operators setting the knob through the host environment or compose
`.env` therefore still get the in-container default.

## Scope

- Update only the local service compose environment pass-through.
- Add focused regression coverage in the existing static compose contract test.
- Do not run broad AWF/GitHub-owned validation, full coverage, or whole-repo
  suites during the agent phase.

## Requirements Checklist

- [ ] `docker/compose/local-service.yml` forwards
  `AWF_CLASSIFIED_ORPHAN_REAP_SCAN_INTERVAL_SECONDS` with a default matching the
  existing orphan reconcile scan interval default.
- [ ] `tests/integration/test_local_service_compose.py` asserts the environment
  key is present for both `api` and `worker`.
- [ ] Focused test fails before the compose change and passes after it.
- [ ] Local validation is limited to the focused compose contract test.

## Implementation Steps

1. Add the missing assertion to the compose environment test.
2. Run that focused test to confirm the missing key fails.
3. Add the environment key beside the other orphan cleanup settings in
   `docker/compose/local-service.yml`.
4. Re-run the same focused test.
5. Record evidence in the validation document.

## Verification

```bash
uv run --python 3.12 --extra dev pytest \
  tests/integration/test_local_service_compose.py::test_local_service_compose_declares_control_plane_stack \
  -q
```
