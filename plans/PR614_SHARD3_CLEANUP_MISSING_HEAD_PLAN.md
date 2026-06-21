# PR614 Shard 3 Cleanup Missing Head Plan

## Problem Statement and Scope

PR #614 also fails GitHub Actions `python-coverage-shards (3)` in run
`27858562982`. The single failure is
`tests/unit/control/test_executor_mirror_hooks_path_commit.py::test_execute_recovers_missing_head_before_agent_cleanup_failure`.
The executor handles `ComposeExecCleanupError` from the agent run by repairing
mirror hooks and then immediately marking the workspace failed. It no longer
checks/recoveres a missing HEAD before preserving the failed workspace, so the
test's recovery mocks are never awaited.

Scope is limited to restoring missing-HEAD recovery in the agent cleanup
failure path before the existing cleanup failure is marked.

## Requirements Checklist

- [ ] Do not switch branches, push, rebase, or run broad AWF/GitHub-owned
      validation.
- [ ] Reproduce the shard 3 failure locally before editing.
- [ ] In the agent `ComposeExecCleanupError` path, keep mirror-hooks cleanup
      repair behavior.
- [ ] If HEAD is missing after cleanup repair, invoke the existing missing-HEAD
      recovery and recovered post-agent verification hooks.
- [ ] Preserve the final cleanup-failure mark so the operator still sees the
      original process cleanup failure.
- [ ] Run focused local tests covering the cleanup missing-HEAD path.
- [ ] Record focused evidence in a validation document and note that full
      AWF/GitHub validation remains owned by AWF after agent completion.
- [ ] Commit the scoped fix locally with a conventional `fix(ci): ...` message.

## Implementation Steps

1. Run the failing shard 3 test locally.
2. Add a small helper in `execution_flow.execute` that verifies HEAD after an
   agent cleanup failure and, if missing, calls
   `_recover_missing_git_head_or_mark_failed` with stage
   `agent_run_cleanup_failure`.
3. After successful recovery, call
   `_verify_recovered_post_agent_commit_or_mark_failed` with the existing
   post-agent verification inputs.
4. Invoke the helper from the agent-run `ComposeExecCleanupError` handler before
   re-raising to the outer cleanup-failure marker.
5. Run the focused failing test.
6. Run narrow lint for touched files.
7. Create `plans/PR614_SHARD3_CLEANUP_MISSING_HEAD_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path_commit.py::test_execute_recovers_missing_head_before_agent_cleanup_failure -q`
  must fail before the fix and pass after.
- Narrow lint over the touched source/test files must pass.
- Do not run full coverage, all unit tests, frontend builds, or CI-equivalent
  validation locally.
