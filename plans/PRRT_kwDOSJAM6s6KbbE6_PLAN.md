# PRRT_kwDOSJAM6s6KbbE6 plan — do not own the entire live worktree delta

## Problem statement

Review thread `PRRT_kwDOSJAM6s6KbbE6` (PR #615,
`src/awf/runtime/pr_monitor_runner/pre_push_validation.py:642`) reports that the
ownership gate added for `PRRT_kwDOSJAM6s6KaUHP` unions the *unstaged
working-tree* delta `git diff --name-status -z operation_start_head` (commit
vs working tree). That diff is the entire live worktree delta since
`operation_start_head`: any tracked file that differs from the anchor —
including one modified after the repair-start dirty guard by a failed cleanup
or another local process — is treated as operation-owned. The gate then passes,
`_commit_dirty_worktree` runs a fresh `git status` and `git add -A --` on every
non-ignored dirty path, so the unrelated edit is staged and committed, and the
post-commit re-validation (added by `PRRT_kwDOSJAM6s6KZP8f`/`Ka0aO`) does not
catch it because the pre-commit `owned_delta_paths` already contained that path.
The previous fail-closed behavior for unrelated dirt (added by
`PRRT_kwDOSJAM6s6KXLaI`) is lost for the working-tree-only case.

The over-broadening came in with `PRRT_kwDOSJAM6s6KaUHP` to fix a real case:
when `_commit_dirty_worktree` returns False because `git add -A` itself failed,
the operation's repair output was never staged, so neither the committed delta
(`operation_start_head..HEAD`) nor the staged delta
(`--cached operation_start_head`) carry it, and the finalize would strand the
operation's own residue as `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`.

Both classes of path look identical at the gate (a working-tree difference vs
`operation_start_head`), and the repair operation does not currently record
the paths it attempted to touch. The KaUHP fix chose the working-tree diff,
which is too broad; this plan scopes the gate to operation-attempted paths
instead of every live worktree difference.

## Scope

- The operation's committed and staged deltas (added by `KXLaI`/`KYd-r`) are
  safe proxies for operation ownership: a failed `_commit_dirty_worktree`
  after a successful `git add -A` leaves the edits staged
  (`--cached operation_start_head`), and a failed `git add -A` *before*
  staging leaves them unstaged in the working tree but NOT staged. The
  working-tree delta is what KaUHP added; it is the source of the
  over-broadening.
- Add the operation's *attempted* staged set as captured by
  `_commit_dirty_worktree` itself: that sink computes `stage_paths` from a
  fresh `git status --porcelain --untracked-files=all` (the set it actually
  tries to `git add -A --`) and that is exactly the operation-attempted set for
  the failed-`git add -A` case. Thread that set out of `_commit_dirty_worktree`
  so the gate (run before the sink) and the post-commit re-validation can use
  it instead of the live working-tree diff.

  This is the minimal change that preserves KaUHP's recovery (operation-owned
  unstaged repair output is still finalized) while restoring KXLaI's
  fail-closed guarantee (unrelated post-guard dirt is not swept in). It is a
  targeted signature change to one commit sink and one gate helper, plus
  plumbing the captured set through `_try_finalize_pre_push_dirty_repair_state`.
- No new abstractions, no unrelated refactor, no protected-file edits.

## Approach detail

`_commit_dirty_worktree` already runs a fresh `git status` and computes
`stage_paths` (the leaf-enumerated, agent-runtime-filtered dirty set it stages).
On the failed-`git add -A` path it returns False *before* reaching the commit,
so the staged/committed deltas are empty but `stage_paths` was computed. The
operation owns exactly those paths: it attempted to add them. Capture
`stage_paths` (the attempted set) and return it alongside the bool so the caller
can use it as the operation-owned unstaged set.

Concretely:
1. `_commit_dirty_worktree` returns `tuple[bool, frozenset[str]]` (committed,
   attempted_paths) instead of `bool`. `attempted_paths` is the `stage_paths`
   it computed (empty when the worktree was clean / status failed / nothing to
   stage / `add -A` failed before staging; non-empty when staging was
   attempted). All existing callers updated to unpack the tuple (or keep bool
   via a thin compatibility helper where the attempted set is unused).
2. `_try_finalize_pre_push_dirty_repair_state` passes `attempted_paths` into
   the ownership gate instead of running the live working-tree diff
   (`git diff --name-status -z operation_start_head`).
3. `_operation_owned_delta_paths` drops the working-tree diff branch and
   unions committed + staged + `attempted_paths` (the operation-attempted set
   threaded from the sink, intersected with the dirty paths the gate already
   holds). `attempted_paths` is captured by the *same* `git status` the sink
   runs, so it never includes unrelated post-guard dirt that the sink did not
   try to stage.

Wait — the gate runs *before* `_commit_dirty_worktree`, so the sink has not
yet computed `stage_paths` when the gate decides. Reconsider:

The gate's job is to decide whether the *current* dirty paths are
operation-owned before committing. The working-tree diff was the KaUHP proxy
for "operation-owned unstaged output". But the sink's `stage_paths` is computed
*inside* the sink, after the gate. So we cannot thread it into the gate without
reordering.

Revised approach — keep the gate's delta sources but make the working-tree
branch operation-attempted, not "every live difference":

The over-broadening is that `git diff operation_start_head` (commit vs working
tree) reports *any* working-tree modification. The operation owns only the
working-tree modifications *it produced*. There is no capture of those, so the
gate cannot distinguish them from unrelated post-guard dirt at decision time.

Given there is no captured set available at gate time, the minimal correct
fix is: **drop the working-tree delta branch from the ownership gate** and
instead have `_commit_dirty_worktree` (the sink) restrict its `git add -A --`
to the operation-owned set the gate already validated (committed + staged +
untracked from `check.untracked_paths`). This restores KXLaI's fail-closed
guarantee for unrelated working-tree-only tracked modifications, at the cost
of re-stranding the KaUHP case (failed `git add -A` leaving operation-owned
unstaged tracked edits).

But re-stranding KaUHP regresses the recovery that thread explicitly asked
for. So the correct fix must keep KaUHP's recovery. Re-examine: is the KaUHP
case actually reachable, and is the over-broadening case reachable?

- KaUHP case: `_commit_dirty_worktree` returns False because `git add -A`
  failed. `git add -A -- <paths>` fails rarely (permissions, disk, pathspec).
  Real but uncommon.
- Over-broadening case: a tracked file modified after the repair-start guard by
  a failed cleanup or another local process. The repair-start guard
  (`_pre_existing_dirty_repair_worktree_result`) runs at repair start; between
  then and pre-push validation the agent CLI ran (can touch files), protected-
  scope repair can run the agent CLI, and cleanup side effects can restore
  files. A tracked modification from any of those that is NOT part of the
  operation's intended edits would be swept in. Real and safety-relevant.

The reviewer's framing ("needs to use paths captured/attempted by the repair
operation") implies capturing the operation's attempted set. The cleanest
minimal capture is to record the paths the operation *staged or committed*,
which the committed+staged deltas already cover for the common case. For the
failed-`git add -A` case, the operation did not successfully stage anything, so
there is no captured set — but the *intent* was to stage the dirty paths
present at repair time. The repair-start guard proved the tree was clean at
`operation_start_head`, so the operation owns every dirty path present
*immediately after the agent run*. That set is not captured either.

Given the absence of a captured set and the safety asymmetry (over-broadening
silently sweeps unrelated dirt into the PR; KaUHP stranding fails closed with a
visible `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`), the safer minimal fix that
honors the reviewer's request is to **remove the working-tree delta branch**
from the ownership gate and accept the KaUHP regression as a documented defer,
*unless* the KaUHP case can be covered another way.

Re-examine the KaUHP case under committed+staged+untracked only (no working-
tree diff): failed `git add -A` leaves the operation's tracked edits unstaged.
The committed delta is empty (HEAD didn't move), the staged delta is empty
(`add -A` failed), and `check.untracked_paths` does not include tracked-modified
paths. So the gate would treat those operation-owned unstaged tracked edits as
unrelated and strand them as `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`. That is
fail-closed (visible, recoverable by a human), not a silent sweep. The KaUHP
recovery is lost, but the over-broadening silent-sweep is also eliminated.

Decision: **remove the working-tree delta branch** from
`_operation_owned_delta_paths` and from the finalize gate. Restore KXLaI's
fail-closed guarantee for unrelated working-tree-only tracked modifications.
The KaUHP recovery (operation-owned unstaged tracked edits from a failed
`git add -A`) regresses to fail-closed `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`;
document this as a defer (a future capture of operation-attempted paths would
restore it without the over-broadening).

This matches the reviewer's explicit ask: "the gate needs to use paths
captured/attempted by the repair operation rather than every live worktree
difference since operation_start_head." The committed and staged deltas ARE
paths the operation captured/attempted (committed = successfully committed;
staged = successfully staged). The live working-tree diff is the part that is
"every live worktree difference", and that is what this fix removes.

## Requirements checklist

- [ ] Add a regression test (TDD red): a tracked file modified after the
      repair-start guard by an unrelated process (present in the working-tree
      delta but NOT committed, NOT staged, and NOT untracked) is NOT swept
      into the PR — the finalize skips and the push fails closed as
      `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`. Currently the KaUHP
      working-tree-delta branch treats it as owned and commits it (the bug).
- [ ] Remove the working-tree delta branch from `_operation_owned_delta_paths`
      (drop the `git diff --name-status -z operation_start_head` diff and its
      `working_tree_delta_unavailable` warning / `None` return).
- [ ] Update the `_operation_owned_delta_paths` and
      `_try_finalize_pre_push_dirty_repair_state` docstrings to remove the
      working-tree delta and cite this thread; restore the KXLaI fail-closed
      framing for unrelated working-tree-only tracked modifications.
- [ ] Update existing finalize tests that queued a working-tree delta result
      to drop that queued result (the gate no longer runs the working-tree
      diff), keeping their asserted behavior where it still holds.
- [ ] The KaUHP test
      `test_pre_push_validation_finalize_commits_operation_owned_unstaged_dirt`
      now asserts fail-closed `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` (the
      documented defer); update its assertion and docstring to record the
      defer rationale.
- [ ] Keep existing finalize tests green: unrelated-dirt fail-closed
      (KXLaI), staged-only recovery (KYd-r), post-commit unowned-delta
      fail-closed (KZP8f), working-tree-only-unowned-dirt not flagged
      post-commit (Ka0aO), rename-source (KaAWk), non-ASCII (KaAWk), untracked
      (Ka0aK), agent-runtime exclusion (Ka0aK), no-anchor, delta-unavailable,
      malformed, policy/ownership/protected-scope/provider reason codes.
- [ ] Targeted lint + typecheck on touched files only.

## Implementation steps

1. Write the red regression test for unrelated working-tree-only tracked dirt.
2. Run it, confirm it fails on the current (KaUHP) code — the finalize commits
   the unrelated path.
3. Remove the working-tree delta branch from `_operation_owned_delta_paths`
   (and its warning/`None` return).
4. Update the two docstrings.
5. Update the KaUHP test to assert fail-closed with defer rationale.
6. Update any other finalize tests that queue a working-tree delta result so
   they no longer queue it (their asserted behavior should still hold).
7. Re-run the full finalize + pre-push validation test files (TDD green).
8. Lint/typecheck touched files.
9. Write `plans/PRRT_kwDOSJAM6s6KbbE6_VALIDATION.md`.

## Verification commands (focused only — broad validation owned by AWF/GitHub)

- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py`

## Pass criteria

- New regression test fails on the KaUHP code (commits unrelated working-tree
  dirt) and passes on the fixed code (fails closed).
- KaUHP test updated to assert fail-closed with documented defer.
- All other finalize + pre-push validation tests green.
- Lint/typecheck clean on touched files.

## Defer note

The KaUHP recovery (operation-owned unstaged tracked edits from a failed
`git add -A`) regresses to fail-closed `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`.
This is the deliberate trade-off: a silent sweep of unrelated dirt into the PR
is worse than a visible fail-closed strand. Restoring KaUHP's recovery without
the over-broadening requires capturing the operation's attempted paths (the
`stage_paths` the sink computes, or the dirty set present immediately after the
agent run) and threading it to the gate — a larger change tracked separately.
