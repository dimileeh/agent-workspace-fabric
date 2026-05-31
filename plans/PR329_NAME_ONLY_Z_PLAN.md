# PR329 name-only -z parser plan

## Problem statement and scope

PR #329 has an unresolved review thread (`PRRT_kwDOSJAM6s6F7PzY`) reporting that
`_changed_paths_from_name_only_z` accepts malformed `git diff --name-only -z`
output. The fix is limited to the PR monitor path parser and its focused unit
coverage.

## Requirements checklist

- Verify whether the current parser accepts non-NUL and unterminated output.
- Add regression coverage showing malformed `--name-only -z` output fails closed.
- Preserve valid behavior for empty output, valid NUL-terminated paths, duplicate
  paths, and empty path rejection.
- Raise `ProtectedScopeDiffError` for malformed `--name-only -z` output.
- Keep validation focused; AWF/GitHub own broad validation after agent
  completion.

## Implementation steps

1. Add focused tests near existing PR monitor parser coverage.
2. Run the targeted tests and confirm the new malformed-output regression fails.
3. Update `_changed_paths_from_name_only_z` to reject non-NUL and unterminated
   output before returning paths.
4. Re-run the same focused tests.
5. Record evidence in `plans/PR329_NAME_ONLY_Z_VALIDATION.md`.
6. Commit only the changed files for this review thread.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py -q`
  - Passes after implementation.
  - Fails before implementation on the new malformed `--name-only -z` regression
    when practical to run.

Broad AWF/GitHub validation is intentionally not run during this agent phase.
