# PRRT_kwDOSJAM6s6GQqGx Plan

## Problem Statement And Scope

The review thread reports that `WorkspaceService.create` can check host-port conflicts under `"local"` when it is constructed with raw `Settings` whose `worker_node_id` is unset, while the worker/provisioner may stamp launched workspaces with its configured effective node id. This can hide an existing workspace that owns the same Docker host port on the actual worker node.

Scope is limited to workspace create admission and the resource reservation node stamped for that created workspace. Branch management, pushing, PR comments, and broad AWF/GitHub validation remain out of scope.

## Requirements Checklist

- Add a regression proving `WorkspaceService.create` checks host-port conflicts using the same effective node id that create-time reservation stamping uses.
- Keep the `"local"` default for local-service/raw unset settings unless the service has a concrete worker node id.
- Ensure explicit `worker_node_id` values still drive both host-port conflict scans and reservation records.
- Run only focused tests for the changed behavior.

## Implementation Steps

1. Add a small service-level regression around host-port conflict detection when an existing active workspace has a non-local node id and the create service is configured with that node id.
2. Centralize the effective workspace create node id so admission and resource reservations use the same value.
3. Update `WorkspaceService.create` and `resource_reservation_plan` to use the shared helper.
4. Run the focused unit tests covering host-port conflict behavior and scheduler reservation records.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_scheduler_records.py tests/unit/service/test_host_port_conflict_helper.py -q`
- Pass criteria: the new regression fails before implementation, passes after implementation, and existing focused host-port/reservation tests remain green.
