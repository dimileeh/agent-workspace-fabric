# PRRT_kwDOSJAM6s6GRzH5 Plan

## Problem Statement

An unresolved PR review thread reports that `destroy_workspace()` skips recording
`workspace.terminal_runtime_released` after successful cleanup for legacy or
partial workspace rows where `compose_file_path` is set but
`compose_project_name` is `NULL`. Those rows can still hold host ports until the
release event is recorded.

## Scope

- Limit changes to the destroy control path and targeted unit coverage.
- Do not push or run broad AWF/GitHub-owned validation.
- Preserve existing behavior for normal rows with `compose_project_name`.

## Requirements

- Add a regression test for successful destroy cleanup with `compose_file_path`
  present and `compose_project_name` absent.
- Confirm the regression test fails before the implementation change when
  practical.
- Emit `workspace.terminal_runtime_released` after successful cleanup when
  either runtime locator is present and the release event is not already
  recorded.
- Include useful release-event payload fields without assuming a compose project
  exists.
- Run focused validation for the touched unit test behavior.

## Implementation Steps

1. Add a unit test beside existing destroy lifecycle tests for the compose-file-only case.
2. Run that single test and capture the expected failure.
3. Update `WorkspaceControlService.destroy_workspace()` release guard/payload.
4. Re-run the targeted test and nearby destroy runtime-release test.
5. Write validation notes in `plans/PRRT_kwDOSJAM6s6GRzH5_VALIDATION.md`.
