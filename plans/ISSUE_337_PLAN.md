# Issue #337 Plan: PR Monitor Pre-Commit Autofix Retry

## Problem Statement and Scope

GitHub issue #337 reports that the PR monitor comment-repair commit path can orphan
pre-commit auto-fix edits. When `git commit` fails because a hook such as
`end-of-file-fixer` modifies a tracked file, the monitor currently returns failure
without re-staging and retrying the commit. The next monitor pass then sees the
leftover dirty path and correctly terminates with
`PRE_EXISTING_DIRTY_WORKTREE`.

The fix is scoped to completing the commit handshake upstream in the PR monitor
repair commit path. The existing conservative dirty-worktree guard must keep its
safety role unchanged.

## Requirements Checklist

- Add regression coverage for a monitor comment-repair commit where a
  pre-commit auto-fixer modifies a tracked file, the monitor re-stages that file,
  retries the commit once, succeeds, and leaves the next guard check clean.
- Add regression coverage for retry failure so the monitor does not mask a failed
  second commit.
- Add regression coverage that non-autofixable or unowned dirty paths are not
  re-staged by the monitor and still hit `PRE_EXISTING_DIRTY_WORKTREE` on the
  next pass.
- Reuse the existing executor pre-commit failure classification rather than
  duplicating hook lists or regular expressions.
- Keep changes scoped to the monitor commit path and any small shared helper
  needed for safe reuse.
- Do not weaken `_pre_existing_dirty_repair_worktree_result`.
- Preserve reason codes and existing failure behavior for unsafe dirty states.

## Implementation Steps

1. Inspect `_commit_dirty_worktree` in
   `src/awf/runtime/pr_monitor_runner/remote_repair.py` and the post-agent
   autofix retry logic in `src/awf/control/executor/quality_methods.py`.
2. Add failing unit tests in the existing PR monitor runtime coverage files for
   the autofix retry success, retry-still-fails, and unsafe dirty path branches.
3. On monitor `git commit` failure, classify the commit output with the executor
   classifier.
4. If the failure indicates `files were modified by this hook`, inspect
   `git status --porcelain` and allow one retry only when current dirty paths are
   non-empty, are reported by the classifier as deterministic/autofix paths, and
   stay within the operation's original dirty scope.
5. Re-stage only the eligible dirty paths and retry `git commit -m <message>`
   once.
6. Return success only when the retry succeeds and existing post-commit ownership
   repair succeeds; otherwise preserve the current failed-commit behavior.
7. Write `plans/ISSUE_337_VALIDATION.md` with requirement status and focused
   verification evidence.

## Verification Commands and Pass Criteria

Focused tests:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py \
  tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py \
  -q
```

If shared classifier code is touched:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_executor_post_agent_commit_classifier.py \
  tests/unit/control/test_executor_post_agent_commit_parts/test_executor_post_agent_commit_part_001.py \
  -q
```

Focused lint/type checks for touched files:

```bash
uv run --python 3.12 --extra dev ruff check \
  src/awf/runtime/pr_monitor_runner/remote_repair.py \
  src/awf/control/executor/quality_gates.py \
  tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py \
  tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py
```

```bash
uv run --python 3.12 --extra dev ruff format --check \
  src/awf/runtime/pr_monitor_runner/remote_repair.py \
  src/awf/control/executor/quality_gates.py \
  tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py \
  tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py
```

```bash
uv run --python 3.12 --extra dev mypy \
  src/awf/runtime/pr_monitor_runner/remote_repair.py \
  src/awf/control/executor/quality_gates.py
```

Pass criteria: focused tests pass, touched-file lint/format/type checks pass or
any environmental blocker is documented, and broad AWF/GitHub validation remains
owned by AWF after agent completion.
