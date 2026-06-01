# PRRT_kwDOSJAM6s6GM1CF Prelaunch Failure Plan

## Problem Statement And Scope

The PR review reports that the provisioner catch-all failure path marks unexpected
pre-launch failures as `compose_launched=True`. That can stamp
`compose_project_name` on a failed workspace whose Docker Compose stack never
started, causing host-port admission to see a false runtime holder.

Scope is limited to `src/awf/node/provisioner.py` and a focused regression test
for pre-launch unexpected failures.

## Requirements Checklist

- Add a regression test where an unexpected exception is raised before
  `WorkspaceStackLauncher.launch()` is reached.
- Assert the workspace fails but leaves `compose_project_name` null.
- Assert the failed never-launched workspace does not block its companion host
  port in `find_host_port_conflicts()`.
- Preserve existing post-launch and `ComposeOperationError` cleanup behavior.
- Keep validation focused; AWF/GitHub owns broad validation after agent exit.

## Implementation Steps

1. Add the failing regression test in the provisioner unit test area.
2. Run only that new targeted test to confirm it fails against current code.
3. Track whether launch has actually been attempted in
   `_provision_claimed_workspace`.
4. Pass that launch-attempt flag into the catch-all `_mark_failed()` call.
5. Re-run the targeted test and any narrow checks needed for the touched files.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py -q -k pre_launch_unexpected`

Pass criteria: the targeted regression passes and shows the failed workspace
does not retain a compose project or block its companion host port.
