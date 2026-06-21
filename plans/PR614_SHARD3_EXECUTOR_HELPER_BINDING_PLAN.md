# PR614 shard 3 executor helper binding plan

## Problem statement and scope

PR #614 has a failing GitHub Actions `python-coverage-shards (3)` job on run
`27846850388`. The actionable failure is
`tests/unit/control/test_executor_parts/test_executor_part_006.py::TestAdapterInitFailure::test_missing_head_before_adapter_init_marks_failed_when_adapter_none`,
which raises `UnboundLocalError` from
`src/awf/control/executor/execution_flow.py` when the executor references the
mirror-hooks repair helper before that local helper is bound.

Scope is limited to fixing the executor helper binding/control-flow bug and
covering it with focused tests. Do not edit workflow or quality-gate
configuration, do not switch branches, and do not run broad AWF/GitHub-owned
validation locally.

## Requirements checklist

- [ ] Preserve AWF branch ownership: no branch switch, push, rebase, or broad
  validation.
- [ ] Reproduce the shard-3 failure with the focused CI test target on the
  current branch when practical.
- [ ] Keep the code change scoped to the executor mirror-hooks helper binding
  path.
- [ ] Preserve fail-closed mirror repair behavior for setup/agent failure paths.
- [ ] Run focused tests for the failing target and nearby executor mirror-hooks
  coverage.
- [ ] Run focused lint for touched files.
- [ ] Record evidence in `plans/PR614_SHARD3_EXECUTOR_HELPER_BINDING_VALIDATION.md`.
- [ ] Commit the fix locally with a conventional commit message.

## Implementation steps

1. Inspect `execution_flow.py` around the failing reference and helper
   definition.
2. Run the focused failing pytest target.
3. Move or otherwise bind the local mirror-hooks repair helper before all
   references without broad executor refactoring.
4. Add or adjust the smallest focused regression test if the existing failing
   target does not already cover the path.
5. Re-run the focused failing target plus nearby mirror-hooks executor tests and
   touched-file Ruff.
6. Write validation notes and commit the scoped change.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_006.py::TestAdapterInitFailure::test_missing_head_before_adapter_init_marks_failed_when_adapter_none -q`
  passes.
- Nearby focused executor mirror-hooks tests pass.
- `uv run --python 3.12 --extra dev ruff check <touched files>` passes.
- Full AWF/GitHub validation and coverage gates remain managed by AWF after
  agent completion.
