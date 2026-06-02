# Legacy Runtime Release Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6GSFhH` reports that operator destroy cleanup skips
`workspace.terminal_runtime_released` for legacy terminal workspaces whose
`compose_project_name` and `compose_file_path` are both null. `WorkspaceCleaner`
still derives the default `awf_<workspace_id>` compose project and attempts
`compose_down`, while host-port conflict detection treats this legacy null-locator
shape as a possible port holder until a release event exists.

Scope is limited to `WorkspaceControlService.destroy_workspace` release-event
recording and focused lifecycle regression coverage.

## Requirements Checklist

- Add a regression test for a destroy cleanup of a null-locator legacy workspace
  that succeeds and records `workspace.terminal_runtime_released`.
- Preserve existing behavior that records release events when stored runtime
  locators exist.
- Preserve partial-cleanup release behavior when `compose_down` succeeds before
  another cleanup step fails.
- Keep validation focused to the touched lifecycle test(s); broad AWF/GitHub
  validation remains managed after agent completion.

## Implementation Steps

1. Add the null-locator destroy regression to the existing controls lifecycle
   test module.
2. Confirm the new regression fails against the current guard.
3. Replace the stored-locator-only predicate with a cleanup-release predicate
   that also covers the legacy default-project cleanup path.
4. Run the focused lifecycle tests that cover normal, compose-file-only, partial,
   and legacy null-locator release behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_002.py -q -k "runtime_released or compose_file_only"`
  passes after the implementation.
- The pre-fix run of the new regression fails because no release event is
  recorded.
