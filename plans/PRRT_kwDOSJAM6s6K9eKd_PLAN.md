# PRRT_kwDOSJAM6s6K9eKd Plan

## Problem Statement and Scope

The review thread reports that mirror hook repair can miss a poisoned
`core.hooksPath` exposed by an `includeIf.gitdir:**/worktrees/**.path` entry in
the shared mirror config. The current mirror probe uses `--git-dir`, which does
not evaluate linked-worktree `gitdir` conditions, and the worktree loop only
repairs `config.worktree`.

Scope is limited to `repair_mirror_hooks_path` behavior and focused regression
coverage for this review thread.

## Requirements Checklist

- Add a regression test for a mirror-level `includeIf.gitdir` that is only
  visible from an attached worktree context.
- Repair poisoned hook config/includes exposed through the shared mirror config
  when evaluated from active linked worktrees.
- Keep existing direct mirror and per-worktree repair behavior intact.
- Run only focused local checks; broad AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Add a failing unit test in `tests/unit/node/test_git_manager_mirror_hooks_repair.py`.
2. Update `repair_mirror_hooks_path` to evaluate the shared mirror config from
   each active worktree context and remove matching include entries from the
   mirror config.
3. Run the focused hook-repair test file.
4. Record validation evidence in `plans/PRRT_kwDOSJAM6s6K9eKd_VALIDATION.md`.
