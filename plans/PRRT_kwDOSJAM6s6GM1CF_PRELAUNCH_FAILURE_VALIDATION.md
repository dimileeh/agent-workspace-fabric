# PRRT_kwDOSJAM6s6GM1CF Prelaunch Failure Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6GM1CF_PRELAUNCH_FAILURE_PLAN.md`

## Requirement Status

- Add a regression test where an unexpected exception is raised before
  `WorkspaceStackLauncher.launch()` is reached: Complete.
- Assert the workspace fails but leaves `compose_project_name` null: Complete.
- Assert the failed never-launched workspace does not block its companion host
  port in `find_host_port_conflicts()`: Complete.
- Preserve existing post-launch and `ComposeOperationError` cleanup behavior:
  Complete for the catch-all launch-attempt path covered by the adjacent
  unexpected-failure test; the `ComposeOperationError` path remains unchanged.
- Keep validation focused; AWF/GitHub owns broad validation after agent exit:
  Complete.

## Evidence

Files changed:

- `src/awf/node/provisioner.py`
- `tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py`
- `plans/PRRT_kwDOSJAM6s6GM1CF_PRELAUNCH_FAILURE_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6GM1CF_PRELAUNCH_FAILURE_VALIDATION.md`

TDD evidence:

- Before implementation, the new regression failed because
  `reloaded.compose_project_name` was `awf_<workspace_id>` instead of `None`.

Focused validation run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py -q -k pre_launch_unexpected
```

Result: `1 passed, 15 deselected`.

Additional narrow checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py -q -k 'pre_launch_unexpected or unexpected_provisioning_failure_marks_workspace_failed'
uv run --python 3.12 --extra dev ruff check src/awf/node/provisioner.py tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py
```

Results: `2 passed, 14 deselected`; `All checks passed!`.

Broad AWF/GitHub validation was not run in the agent phase per the workspace
contract.
