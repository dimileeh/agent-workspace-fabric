# Issue #327: Post-agent commit fails when agent self-commits

## Problem

When an AWF agent commits its own work during the agent run (clean worktree, branch ahead of base), the post-agent `git commit` finds nothing to commit, exits 1 with "nothing to commit, working tree clean", and the executor raises `_PostAgentCommitStepError` → `POST_AGENT_COMMIT_FAILED`. The branch is perfectly valid (ahead of base) but the workspace is failed as `infrastructure_failure`.

## Root cause

`execution_flow.py` lines ~837-863: when `git commit` returns non-zero, the code classifies the failure and either repairs or raises `_PostAgentCommitStepError`. This raise happens **before** the rev-list check (~lines 864-905) that would determinate whether the branch actually advanced past base.

When the agent self-commits:
- `git add -A` → ok (no unstaged changes)
- `git diff --cached --name-only` → empty (nothing staged)
- `git commit` → exit 1, "nothing to commit, working tree clean"
- → `_PostAgentCommitStepError` raised → workspace fails

The comment at line 475 confirms the intended design: "If there's nothing to commit, the existing no-work check fails the workspace with `agent_failure` below. If there IS work, validation decides whether it's pushable." The commit-failure path just raises too early.

## Fix

In `execution_flow.py`, when `git commit` fails specifically because there is nothing to commit (detect from output: "nothing to commit" in stdout/stderr, working tree clean), do NOT raise `_PostAgentCommitStepError`. Instead, fall through to the existing `git rev-list --count <base_commit>..HEAD` check:

- **rev-count > 0**: agent already committed work → proceed to validation → push → PR
- **rev-count == 0**: genuinely no work → existing `agent_failure` path with clear message

Preserve the distinction:
- **Nothing-to-commit** (benign): fall through to rev-list check
- **Real git commit error** (e.g., empty ident, detached HEAD): still raise `_PostAgentCommitStepError` → `POST_AGENT_COMMIT_FAILED`
- **git add failure**: unchanged, still fatal

No new parallel paths — reuse the existing rev-list/orphan-history logic.

## Changes

### `src/awf/control/executor/execution_flow.py`

After `commit_result.ok` is False (line ~837):
1. Classify the failure as before.
2. Check if the failure is specifically "nothing to commit" using a new helper `_is_nothing_to_commit`.
3. If it IS nothing-to-commit: log it, skip the error/repair path, fall through to rev-list check.
4. If it is NOT nothing-to-commit: existing error/repair logic unchanged.

### `src/awf/control/executor/quality_gates.py`

Add `_is_nothing_to_commit(result: CommandResult) -> bool` helper that checks for "nothing to commit" patterns in commit output. This keeps the detection logic testable and co-located with the other commit classification logic.

### Tests (regression — TDD)

In a new test class in the existing error-paths test file:

1. **Agent self-committed, branch ahead**: `git commit` exits 1 with "nothing to commit", `git rev-list --count` returns > 0 → workspace proceeds to push/PR (NOT failed).
2. **Clean tree, branch NOT ahead**: `git commit` exits 1 with "nothing to commit", `git rev-list --count` returns 0 → workspace fails as `agent_failure` with clear message.
3. **Real git commit error**: `git commit` exits 1 with "fatal: empty ident name" → still fails as `POST_AGENT_COMMIT_FAILED`.

Update existing `test_nonzero_git_commit_raises_and_marks_failed` to use a real error (not "nothing to commit").

### Classifier test addition

Add a test for `_is_nothing_to_commit` in the classifier test file.

## Acceptance criteria

- [ ] Agent self-commits (clean tree, branch ahead) → workspace proceeds
- [ ] No work (clean tree, branch not ahead) → `agent_failure` with clear message
- [ ] Real git error → `POST_AGENT_COMMIT_FAILED`
- [ ] Orphan history guard still runs for self-committed branches
- [ ] Regression tests cover all branches
- [ ] Line count guard (<1500) not violated
