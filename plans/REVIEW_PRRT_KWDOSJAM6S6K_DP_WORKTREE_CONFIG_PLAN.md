# Review PRRT_kwDOSJAM6s6K-dP- Worktree Config Plan

## Problem Statement and Scope

The mirror hooks-path repair currently probes a linked worktree's
`config.worktree` with `git config --worktree` whenever the file exists. Git only
honors that file when the shared repository config enables
`extensions.worktreeConfig`; otherwise `--worktree` fails and a stale file can
turn unrelated setup/agent/push repair into `MIRROR_HOOKS_PATH_REPAIR_FAILED`.

Scope is limited to the mirror hooks-path repair in `src/awf/node/git_manager.py`
and focused unit coverage in `tests/unit/node/test_git_manager_mirror_hooks_repair.py`.

## Requirements Checklist

- Add a regression test for a linked worktree that has a stray
  `config.worktree` while `extensions.worktreeConfig` is unset.
- Preserve existing repair behavior when `extensions.worktreeConfig=true`.
- Skip ignored `config.worktree` files before invoking `git config --worktree`.
- Keep changes minimal and avoid broad validation in the agent phase.

## Implementation Steps

1. Add a focused failing test beside existing mirror hooks repair worktree tests.
2. Add a small helper that determines whether Git will honor worktree config for
   a linked worktree.
3. Gate the `config.worktree` repair call on that helper.
4. Run the targeted mirror hooks repair unit tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py -q`
  passes.
- Full AWF/GitHub validation remains managed by AWF after agent completion.
