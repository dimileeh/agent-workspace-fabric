# Review Thread PRRT_kwDOSJAM6s6GYku9 Reserved Legacy Launch Plan

## Problem Statement And Scope

The retry runtime-release gate currently treats a source `ResourceReservation`
as proof that a failed workspace with null Compose metadata never launched.
The review thread reports this is unsafe for upgraded legacy
`ComposeOperationError` failures: those rows may have a reservation while the
old `awf_<workspace_id>` Compose stack still holds requested host ports.

Scope is limited to retry admission in `src/awf/service/workspaces_retry.py`
and focused regression tests in `tests/unit/service/test_workspace_retry_port.py`.

## Assumptions/Changes

- During implementation, the safe retry path needed a durable pre-launch
  marker for future provisioner failures. Scope was expanded narrowly to add
  `workspace.pre_launch_failed` provenance in `src/awf/node/provisioner.py`
  and constants in `src/awf/db/repositories/base.py`.

## Requirements Checklist

- Add a regression for a failed source with host ports, null Compose metadata,
  a reservation, and launch-phase evidence that must be rejected until terminal
  runtime release.
- Preserve the existing safe pre-launch retry path for failures that have not
  reached launch-phase evidence.
- Record durable pre-launch failure evidence for future failures where Compose
  did not start.
- Treat reservations as node/placement evidence, not definitive pre-launch
  proof.
- Keep the source-exclusion host-port conflict invariant documented.
- Run only focused tests for the changed retry-port behavior; full AWF/GitHub
  validation remains managed after agent completion.

## Implementation Steps

1. Add the failing regression around the reserved legacy launch-failure case.
2. Add a pre-launch failure marker emitted before failed transition when
   Compose launch did not start.
3. Update the retry runtime-release helper to require pre-launch evidence for
   null-runtime terminal rows.
4. Adjust comments/docstrings to reflect the new admission rule.
5. Run the targeted retry-port and provisioner marker test(s).
6. Create the validation document with requirement status and evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_006.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces_retry.py src/awf/node/provisioner.py src/awf/db/repositories/base.py tests/unit/service/test_workspace_retry_port.py tests/unit/node/test_provisioner_parts/test_provisioner_part_006.py`

Pass criteria: the focused retry-port tests pass locally. Broad validation,
coverage gates, and CI-equivalent suites are intentionally left to AWF/GitHub.
