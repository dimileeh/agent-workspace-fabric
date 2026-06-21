# Problem Statement

Review thread `PRRT_kwDOSJAM6s6K1vOL` reports that the missing-HEAD recovery
path runs the post-recovery `git diff --name-status` through the command runner
without stripping inherited `GIT_OBJECT_DIRECTORY` and
`GIT_ALTERNATE_OBJECT_DIRECTORIES`. That diff drives the decision to skip
runtime-only recovered changes and to run ownership/protected-scope gates, so it
must use the canonical worktree object database.

# Scope

- Limit changes to the recovered-diff command in
  `src/awf/runtime/pr_monitor_runner/remote_repair.py`.
- Add focused regression coverage in the existing missing-HEAD recovery tests.
- Do not run broad AWF/GitHub validation; AWF owns full validation after the
  agent phase.

# Requirements Checklist

- [ ] Recovered diff command uses an environment with Git object lookup
      overrides removed.
- [ ] Regression test fails before the implementation when object lookup
      overrides are inherited.
- [ ] Targeted test passes after the implementation.
- [ ] Commit the scoped fix locally with a conventional commit message.

# Implementation Steps

1. Add assertions to the focused missing-HEAD recovered-diff test that set the
   Git object lookup override environment variables and require the diff runner
   call to receive a sanitized env.
2. Run that one test and confirm it fails before the code change.
3. Pass `git_env_without_object_lookup_overrides()` into the recovered-diff
   runner call.
4. Re-run the focused test.
5. Record validation evidence in
   `plans/PRRT_kwDOSJAM6s6K1vOL_VALIDATION.md`.

# Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_missing_head_recovery_runtime_only_returns_false -q`

Pass criteria: the targeted regression fails before implementation because the
diff call has no sanitized env, then passes after the implementation.
