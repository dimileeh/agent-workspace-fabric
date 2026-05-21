# Preserved Active Worktree Unknown Plan

## Problem Statement and Scope

Inline review thread `PRRT_kwDOSJAM6s6DlTk3` reports that restart recovery treats an unavailable preserved-active worktree path as `no_work`. That can create a replacement workspace and cancel the preserved active workspace even though local committed work may still exist but cannot currently be located.

Scope is limited to preserved-active restart recovery classification in `src/awf/control/worker.py` and focused unit coverage in `tests/unit/control/test_worker.py`.

## Requirements Checklist

- Classify an unavailable or non-`Path` preserved-active worktree root as `ambiguous`, not `no_work`.
- Preserve existing `no_work` replacement behavior when a known worktree path is available but the path is missing.
- Add or update regression coverage proving unavailable path recovery requires operator handling instead of replacement.
- Keep changes scoped and avoid unrelated refactors.

## Implementation Steps

1. Update the direct classification regression for unavailable worktree roots to expect `ambiguous`.
2. Adjust preserved-active no-work replacement tests so they use a known missing worktree path, not an unknown path.
3. Change `_classify_preserved_active_worktree` so `worktree_path is None` returns `ambiguous` with the existing `worktree_root_unavailable` reason.
4. Run focused tests around preserved-active unknown/no-work recovery.
5. Run broader lint/type/unit validation if time and dependencies permit.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_unavailable_worktree_root or preserved_active_without_usable_work"`
  - Passes and demonstrates unknown path is ambiguous while known missing worktree still creates replacement.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_ambiguous_dirty_worktree or preserved_active_git_status_failure or preserved_active_missing_branch_name"`
  - Passes adjacent preserved-active operator/retry behavior.
