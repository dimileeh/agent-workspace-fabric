# Plan — Re-block directive leak before asking for a grant

PR #609, chatgpt-codex-connector P2 thread on
`src/awf/runtime/pr_monitor_runner/operator_hints.py:293`.

## Problem

The directive-leak guard in `_run_operator_hint_cycle`
(`operator_hints.py`, ~line 260) fires when a DIRECTIVE resume resolves a
protected-scope block by adding a revert commit ON TOP of the preserved
offending commit (the net diff is clean, so `_protected_scope_push_block`
returns `None`) while the preserved ungranted protected commit is still in the
unpushed range. Today it calls `mark_operator_hint_needs_human(...)` and returns
a NON-failed result, parking the workspace at `monitoring_pr`. The reason text
tells the operator to "approve-and-keep the protected path, then resume".

But `guide_workspace` only accepts `--grant` inputs when the workspace is
`blocked` (`controls_guide.py`: non-blocked + grants ->
`WorkspaceGuideGrantNotAllowedError`). So the advertised approve-and-keep grant
is **unreachable** after this guard fires — the operator can never resolve it.

## Fix

When the leak guard fires, RE-BLOCK the workspace (reuse the shared WS-1
`enter_blocked` pause path `_pause_monitor_for_protected_scope_block`) instead of
parking a needs_human hint at `monitoring_pr`. The preserved commit is kept; the
workspace transitions `monitoring_pr -> blocked` so `guide --grant` is accepted,
and the existing monitor-origin blocked-resume machinery
(`_guide_monitor_origin_blocked_workspace` -> `blocked -> monitoring_pr`
grant-only resume -> grant-aware push) resolves it.

The pause path needs the protected violations; the net-diff block is `None`
here, so rebuild them from `workspace.block_violations` (recorded by the
original block — exactly the protected paths the preserved commit changed) via a
new helper `_directive_preserved_leak_protected_block`. Preserve the resume
ORIGIN exactly as the existing net-diff re-block branch
(sync-base vs generic push phase). When `paused_into_blocked`, clear the
in-memory monitor hint (mirrors the net-diff re-block branch).

Fallback: if no violation can be reconstructed (defensive; a real protected
block always records >=1 violation), keep the prior NON-failed needs_human park
so a recoverable workspace is never `_terminate_failed`ed
(PRRT_kwDOSJAM6s6KHEEU).

## Tests

- New: directive leak with recorded block_violations RE-BLOCKS (paused, hint
  cleared, captured block carries the recorded violation), preserving the resume
  phase.
- New regression: after the guard fires and re-blocks (real pause -> DB
  `blocked`), an operator `guide --grant` is accepted and resolves the workspace
  (`blocked -> monitoring_pr`, grant recorded).
- Updated: `test_operator_hint_directive_with_uncovering_grant_still_leaks_needs_human`
  now asserts the re-block resolution (the push still must not run — a
  partial/mismatched grant does not authorize the leak).
- Unchanged: `test_operator_hint_directive_revert_on_top_leaks_preserved_commit_needs_human`
  seeds no block_violations -> exercises the defensive needs_human fallback.

## Validation

Focused: `pytest tests/unit/runtime/test_pr_monitor_operator_hints_part_003.py`
and the new cases; focused ruff/mypy on the touched files. Full AWF/GitHub
validation is owned by AWF after the agent completes.
