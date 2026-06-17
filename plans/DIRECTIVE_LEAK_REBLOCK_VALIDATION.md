# Validation — Re-block directive leak before asking for a grant

PR #609, chatgpt-codex-connector P2 thread on
`src/awf/runtime/pr_monitor_runner/operator_hints.py:293`.

## What changed

- `src/awf/runtime/pr_monitor_runner/operator_hints.py`
  - The directive-leak guard in `_run_operator_hint_cycle` no longer parks a
    needs_human hint at `monitoring_pr` (where `guide --grant` is rejected).
    It now RE-BLOCKS the workspace via the shared WS-1
    `_pause_monitor_for_protected_scope_block` pause path, preserving the resume
    ORIGIN (sync-base vs generic) exactly as the net-diff re-block branch above,
    and clears the in-memory monitor hint when `paused_into_blocked`.
  - New helper `_directive_preserved_leak_protected_block` rebuilds the
    `_ProtectedScopePushBlock` from `workspace.block_violations` (the protected
    paths recorded at the original block), using the leak reason as the block
    message. Returns `None` when nothing can be rebuilt -> caller falls back to
    the prior NON-failed needs_human park (defensive; a real block always records
    >=1 violation).

## Why this is correct

`guide_workspace` only accepts `--grant` inputs for a `blocked` workspace
(`controls_guide.py`: non-blocked + grants -> `WorkspaceGuideGrantNotAllowedError`).
After the leak guard fired and parked at `monitoring_pr`, the advertised
"approve-and-keep the protected path" was unreachable. Re-blocking makes it
reachable: `guide --grant` -> `_guide_monitor_origin_blocked_workspace`
(`blocked -> monitoring_pr`, grant recorded) -> grant-only resume -> grant-aware
push consumes the grant and lands the preserved commit. The bumped block epoch
invalidates any stale partial grant, so the operator must re-grant the actual
violation path.

The anti-leak guarantee is preserved: a directive resume with a partial/mismatched
grant still does NOT push (re-blocks instead).

## Focused checks run (AWF/GitHub own broad validation)

- `pytest tests/unit/runtime/test_pr_monitor_operator_hints_part_003.py` — 14 passed
- `pytest .../part_002.py .../part_003.py .../part_004.py
  tests/unit/service/.../test_controls_lifecycle_guide_blocked.py` — 72 passed
- `ruff check` + `ruff format --check` on touched files — clean
- `mypy` (repo-pinned `files = ["src/"]`) — Success, no issues

### Tests

- New `test_directive_leak_reblock_lets_operator_grant_resolve_workspace`:
  end-to-end — the guard fires, the REAL pause transitions the workspace to
  `blocked` in the DB, then an operator `guide --grant` is accepted and resolves
  it (`blocked -> monitoring_pr`). This is the regression the hint requested.
- Updated `test_operator_hint_directive_with_uncovering_grant_reblocks_for_grant`
  (was `..._still_leaks_needs_human`): a partial/mismatched grant now re-blocks
  (rebuilt from recorded `block_violations`), push still must not run. The
  anti-leak assertion is preserved/strengthened, not weakened.
- Unchanged `test_operator_hint_directive_revert_on_top_leaks_preserved_commit_needs_human`:
  seeds no `block_violations`, so it now exercises the defensive needs_human
  fallback (still NON-failed, hint retained).

### Coverage reasoning

New helper branches are exercised: empty-path skip (empty entry in the
uncovering-grant test), `return None` on no violations (revert-on-top test),
full section/line/protected_pattern/reason reconstruction (grant-resolution
test). The `workspace is None` arm is `# pragma: no cover` — the active monitor
cycle always holds the row. Full coverage gate is owned by AWF/CI after agent
completion.
