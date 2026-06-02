# Host Port Reserved Legacy Launch Plan

## Problem Statement And Scope

An unresolved review thread reports that `find_host_port_conflicts()` skips
terminal workspaces whose Compose metadata is null once any
`ResourceReservation` exists. Some upgraded legacy launch failures can have a
reservation while still leaking an `awf_<workspace_id>` Compose runtime, so
their declared companion/profile host ports must block admission until cleanup
records an effective terminal-runtime release.

Scope is limited to host-port conflict scanning and focused regression tests.

## Requirements Checklist

- Add a regression for a terminal null-compose workspace with a reservation
  whose declared host ports are reported as conflicts before cleanup release.
- Preserve the modern pre-launch escape hatch: a null-compose terminal row with
  durable `workspace.pre_launch_failed` evidence does not block port reuse.
- Keep existing node-scoped reservation behavior intact.
- Do not run broad AWF/GitHub-owned validation; record focused checks only.

## Implementation Steps

1. Add failing repository-level tests in the host-port collision test module.
2. Update `find_host_port_conflicts()` so reserved null-compose terminal rows
   are included unless explicit pre-launch failure evidence exists.
3. Update nearby comments/docstring to describe the reservation plus
   pre-launch-evidence distinction.
4. Run the focused failing/passing tests and targeted lint for changed files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/workspace_repo_host_ports.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py`
  passes.
- Full AWF/GitHub validation is managed after agent completion and is not run
  inside this workspace phase.
