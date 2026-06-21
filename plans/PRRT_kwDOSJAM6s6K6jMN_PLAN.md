# PRRT_kwDOSJAM6s6K6jMN Plan

## Problem Statement and Scope

The inline review thread reports a test coverage gap in
`test_commit_dirty_worktree_strips_git_object_env_from_write_path`: the test
asserts poisoned Git object lookup environment variables are removed, but it
does not assert unrelated Git/AWF environment variables are preserved.

Scope is limited to the cited unit test and plan/validation evidence for this
review thread.

## Requirements Checklist

- Verify the review claim against the current test and sanitizer behavior.
- Preserve existing negative assertions for `GIT_OBJECT_DIRECTORY` and
  `GIT_ALTERNATE_OBJECT_DIRECTORIES`.
- Add a positive assertion that a legitimate environment variable is still
  present in sanitized git command environments.
- Keep validation focused to the changed test only; broad AWF/GitHub validation
  remains owned by AWF after agent completion.

## Implementation Steps

1. Inspect the cited test and `git_env_without_object_lookup_overrides`.
2. Set a representative legitimate Git identity environment variable in the
   test with `monkeypatch`.
3. Assert that the variable and value are present in every recorded sanitized
   git call environment while retaining the existing poison-removal checks.
4. Run the targeted pytest for the single changed test.
5. Record validation results in `plans/PRRT_kwDOSJAM6s6K6jMN_VALIDATION.md`.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_strips_git_object_env_from_write_path -q
```

Pass criteria: the targeted test passes and confirms both removal of poisoned
Git object lookup environment variables and preservation of the legitimate
Git identity variable.
