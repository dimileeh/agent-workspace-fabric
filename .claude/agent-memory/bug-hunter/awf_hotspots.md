---
name: Recurring bug hotspots in AWF
description: Bug patterns that keep showing up across AWF PRs — check these first on scans
type: project
---

Recurring bug patterns seen in AWF scans:

1. **`GitManager` per-task instantiation in `scripts/run_awf.py`** — a fresh `GitManager` is built inside every `_run_task` / `_run_sync_*`. Its per-repo `_mirror_locks` dict is an INSTANCE attribute; two concurrent tasks on the same repo hold different locks, so the "serialize concurrent ensure_mirror / worktree add" guarantee evaporates. Race condition window: mirror-clone vs mirror-clone, worktree add vs worktree add.

2. **Hardcoded `_ROOT / ".venv" / "bin" / "python"`** — CodeRabbit already flagged this in `schedule_release_pr.py` (fixed → `sys.executable`). Same pattern still in `scripts/attach_feature_pr_monitor.py` (as of 2026-04-24).

3. **Review-level comment "fix_committed" verdicts never resolved on GitHub** — `_run_fix_cycle` in `pr_monitor_runner.py` only appends INLINE thread IDs to `threads_to_resolve`. Review-level comments stay unresolved even when the CLI says "fixed"; they're only filtered out of future batches via `state.threads_addressed_ids`. This is documented as intentional (review-level comments have no thread_id to `resolveReviewThread` against), but the behavior is surprising — the `NotifyHuman` gate can flip on a bot-authored defer that the CLI claimed to fix.

4. **Signal handling in asyncio via `signal.signal()`** — `release_pr_watcher.py` uses `signal.signal(SIGTERM, handler)` instead of `loop.add_signal_handler(...)`. Handler calls `asyncio.Event.set()`, which is not signal-safe. Usually works, occasionally wedges.

5. **`_run_sync_base` pushes after CLI-conflict-resolution failure** — If the CLI fails to resolve merge conflicts (AgentRunError), `_run_sync_base` logs and STILL calls `_git_push`. HEAD hasn't advanced so the push is a no-op for the remote, but the local worktree is left with unmerged index state until next iteration's `git merge --abort`.

6. **`_parse_verdict` returns "defer" on empty stdout** — If the CLI writes only to stderr (or a signal-killed CLI dumps nothing), we force a "defer". Rare, but a bot-reviewed PR with no stdout activity will spuriously collect human-defer markers that block merge.

**Why:** Each of these has bitten a real PR review. Future scans should confirm they haven't regressed AND check neighbouring files for the same pattern.

**How to apply:** When scanning AWF, grep for: `GitManager(`, `_ROOT / ".venv"`, `signal.signal(`, `threads_to_resolve.append`, `_run_sync_base`. These are the first five places to check.
