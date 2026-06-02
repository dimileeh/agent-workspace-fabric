# PRRT_kwDOSJAM6s6GbCqf Compose Launch Signal Plan

## Problem Statement

The provisioner currently sets `stack_launch_started = True` immediately before
calling `WorkspaceStackLauncher.launch()`. The real `ComposeStackLauncher`
performs pre-Compose work inside `launch()` before Docker Compose is actually
started. Exceptions in that pre-up work can therefore be treated as launched
runtime failures, leaving `compose_project_name` persisted and causing host-port
admission to treat ports as occupied even though no stack started.

## Scope

- Keep post-compose failure handling intact: if Docker Compose was attempted,
  retain cleanup metadata and diagnostics.
- Treat pre-compose launcher failures as pre-launch failures, including clearing
  pre-published `compose_project_name`.
- Preserve existing terminal-cleanup race handling for actual compose-up
  failures.
- Do not run broad AWF/GitHub validation; AWF owns that after agent completion.

## Requirements Checklist

- Add a launcher-to-provisioner signal that fires only when the real compose-up
  attempt starts.
- Drive unexpected and `ComposeOperationError` failure cleanup from that signal.
- Add regression coverage for a pre-compose launcher failure after the
  pre-launch metadata commit.
- Update existing compose-failure tests to explicitly model an attempted
  compose-up.
- Record focused validation commands and pass criteria.

## Implementation Steps

1. Add failing tests showing pre-compose launcher exceptions clear
   `compose_project_name` and do not block host-port admission.
2. Add an optional compose-up-started callback to the stack launch request and
   invoke it from the compose launch path at the first compose-up attempt.
3. Remove the provisioner's eager `stack_launch_started = True` assignment and
   set the flag only via the callback.
4. Change pre-up `ComposeOperationError` and unexpected launcher failures to
   clear unlaunched compose metadata, while keeping launched compose failures on
   the existing cleanup/diagnostic path.
5. Run targeted provisioner and stack launcher tests that cover the changed
   behavior.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py tests/unit/node/test_provisioner_parts/test_provisioner_part_006.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_edge_cases.py -q`

Pass criteria: all targeted tests pass, and the validation document notes that
full AWF/GitHub validation is intentionally left to AWF after this agent phase.
