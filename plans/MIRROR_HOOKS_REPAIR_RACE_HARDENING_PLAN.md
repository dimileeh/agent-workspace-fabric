# Mirror Hooks Repair Race Hardening

## Summary
Harden AWF's mirror hooks-path repair after a PR monitor failed post-validation with
`MIRROR_HOOKS_PATH_POISONED` while another workspace was removing/pruning a worktree
on the same mirror. The fix extends PR #614 by serializing mirror repair with the
same per-mirror lock used for worktree add/remove/prune, preserving fail-closed
behavior for real surviving hooks-path poisoning, and recording actionable repair
evidence when repair still fails.

## Implementation
- Make `repair_mirror_hooks_path()` acquire the shared mirror lock before reading
  or mutating mirror/worktree Git metadata.
- Treat disappearing linked-worktree metadata as stale mirror metadata: skip it,
  prune under the same lock, and run one follow-up repair scan.
- Keep repair strict when a `core.hooksPath` remains after repair or Git config
  commands fail for non-stale reasons.
- Add a shared PR-monitor evidence helper for mirror repair failures and use it in
  pre-push validation paths so workspace events/logs include operation, returncode,
  stdout, stderr, stage, and mirror path.

## Validation
- Add focused Git manager coverage for mirror-lock serialization and disappearing
  linked-worktree metadata.
- Add PR monitor coverage proving post-validation repair failure details are exposed
  in result details.
- Run targeted pytest, ruff, and mypy on touched files.
