# Review Thread PRRT_kwDOSJAM6s6GYku9 Reserved Legacy Launch Validation

Plan reference:
`plans/review_thread_PRRT_kwDOSJAM6s6GYku9_reserved_legacy_launch_PLAN.md`

## Requirement Status

- Complete: Added a regression for a failed source with host ports, null
  Compose metadata, a reservation, and launch-phase evidence that must be
  rejected until terminal runtime release.
- Complete: Preserved the safe pre-launch retry path by requiring explicit
  `workspace.pre_launch_failed` evidence instead of treating a reservation as
  proof that Compose never launched.
- Complete: Added durable pre-launch failure provenance from the provisioner
  when `compose_launched` is false.
- Complete: Updated retry admission comments so source-exclusion safety depends
  on terminal runtime release or definitive pre-launch evidence.
- Complete: Ran focused validation only; broad AWF/GitHub validation remains
  managed after agent completion.

## Evidence

Files changed:

- `src/awf/db/repositories/base.py`
- `src/awf/node/provisioner.py`
- `src/awf/service/workspaces_retry.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_006.py`
- `tests/unit/service/test_workspace_retry_port.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_rejects_null_compose_source_with_reservation_after_launch_started -q`
  - Failed before implementation as expected.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_006.py::TestFailureHandlingEdgesPart006::test_pre_launch_unexpected_failure_does_not_claim_runtime_ports -q`
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py -q`
  - Passed: 22 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_006.py -q`
  - Passed: 6 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces_retry.py src/awf/node/provisioner.py src/awf/db/repositories/base.py tests/unit/service/test_workspace_retry_port.py tests/unit/node/test_provisioner_parts/test_provisioner_part_006.py`
  - Passed.
