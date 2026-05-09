# Troubleshooting

## Failure Handling

AWF uses coarse failure reasons today:

- `agent_failure`
- `validation_failure`
- `infrastructure_failure`
- `policy_failure`
- `cleanup_failure`
- `profile_resolution_failure`
- `service_startup_failure`
- `phase_timeout`
- `health_check_failure`

Successful workspaces are torn down after completion. Failed workspaces are
preserved so an operator can inspect containers, logs, worktrees, and artifacts.
