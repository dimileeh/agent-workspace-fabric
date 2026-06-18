# Validation: PRRT_kwDOSJAM6s6KcSj — do not own every untracked path

Plan reference: `plans/PRRT_kwDOSJAM6s6KcSj_PLAN.md`

## Requirement-by-requirement status

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| 1 | TDD red: regression test asserting an unrelated purely-untracked path is NOT committed (finalize skips, push fails closed as `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`) | Complete | `test_pre_push_validation_finalize_skips_unrelated_untracked_dirt` — confirmed red before fix (fold-in committed the untracked file, mock ran out of side effects). Green after fix. |
| 2 | Remove the `check.untracked_paths` fold-in line and its comment block | Complete | `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` — `owned_delta_paths | set(check.untracked_paths)` and the `Ka0aK` comment block removed; replaced with a comment explaining the removal and citing `KcSj`. |
| 3 | Update `_operation_owned_delta_paths` docstring to drop the untracked fold-in framing and cite `KcSj` | Complete (N/A) | `_operation_owned_delta_paths` docstring never mentioned the untracked fold-in (the fold-in lived in `_try_finalize_pre_push_dirty_repair_state`). No edit needed there. |
| 4 | Update `_try_finalize_pre_push_dirty_repair_state` docstring: remove untracked fold-in paragraph, cite `KcSj` for fail-closed restoration, record `Ka0aK` defer | Complete | New paragraph added to the finalize docstring mirroring the `KbbE6`/`KaUHP` working-tree-delta paragraph, citing `KcSj` and the `Ka0aK` defer. |
| 5 | Convert `test_pre_push_validation_finalize_commits_operation_owned_untracked_dirt` to fail-closed defer with updated docstring | Complete | Renamed to `test_pre_push_validation_finalize_strands_operation_owned_untracked_dirt_fail_closed`; docstring records the `Ka0aK` regression, the `KcSj` reason, and the deferred follow-up; assertions flipped to fail-closed (`passed is False`, `reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`, `commit_dirty.assert_not_awaited()`). |
| 6 | Keep `test_pre_push_validation_finalize_excludes_agent_runtime_untracked_dirt` green unchanged | Complete | Unchanged. It asserts fail-closed for a suppressed agent-runtime artifact whose `untracked_paths` is empty; the owned set is empty either way (with or without the fold-in), so removing the fold-in does not change its outcome. Still green. |

## Evidence (commands run)

Focused checks (AWF/CI owns broad validation per workspace contract):

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py
# -> All checks passed!

uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py
# -> Success: no issues found in 1 source file

uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py -q
# -> 27 passed

uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs_validated_push.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q
# -> 31 passed

uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/ -q
# -> 283 passed
```

## Files changed

- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` — removed the `check.untracked_paths` fold-in and its `Ka0aK` comment block; added a removal comment citing `KcSj`; added a docstring paragraph to `_try_finalize_pre_push_dirty_repair_state`.
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py` — converted `..._commits_operation_owned_untracked_dirt` to `..._strands_operation_owned_untracked_dirt_fail_closed`; added new regression test `..._skips_unrelated_untracked_dirt`.
- `plans/PRRT_kwDOSJAM6s6KcSj_PLAN.md` — this plan.
- `plans/PRRT_kwDOSJAM6s6KcSj_VALIDATION.md` — this validation.

## Deferred follow-ups

- **Restore `Ka0aK`/`KaUHP` recovery without over-broadening.** Capturing the
  operation's attempted untracked and unstaged paths (e.g. the `stage_paths`
  the commit sink computes, captured before the repair-start guard window can
  be violated) and threading them into the ownership gate would restore the
  recovery for genuinely operation-owned purely-untracked repair output and
  operation-owned unstaged tracked edits from a failed `git add -A`, without
  treating every current untracked/working-tree path as owned. This is the
  same defer recorded in `plans/PRRT_kwDOSJAM6s6KbbE6_PLAN.md`. Out of scope
  for this comment fix.

## Completion

All planned requirements Complete. No Partial/Missing items. Full AWF/GitHub
broad validation (coverage gate, full suite) is owned by AWF after agent
completion and was not run in the agent phase per the workspace contract.
