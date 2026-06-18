# PRRT_kwDOSJAM6s6KYd-r pre-push dirty finalize operation-owned staged paths validation

## Result
Implemented the operation-owned staged-delta union fix from
`plans/PRRT_kwDOSJAM6s6KYd-r_PLAN.md`. The pre-push dirty finalize
ownership gate now unions the operation's committed delta
(`git diff --name-only operation_start_head..HEAD`) with its staged delta
(`git diff --name-only --cached operation_start_head`), so operation-owned
residue left staged by a failed `_commit_dirty_worktree` (empty committed
delta, HEAD never moved) is finalized instead of being stranded as
`VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`.

## Root-cause coverage
- Review thread `PRRT_kwDOSJAM6s6KYd-r`: the previous gate only considered
  the committed delta. When the operation's `_commit_dirty_worktree`
  returned False before creating a commit (e.g. `git commit` failed after
  `git add -A`), `operation_start_head..HEAD` was empty, so every
  operation-owned dirty path was treated as unrelated and the finalize was
  skipped. The staged union makes the operation own the paths it attempted to
  commit, closing the over-conservative fail-closed gap the reviewer raised.
- The fail-closed guarantees from `PRRT_kwDOSJAM6s6KXLaI` are preserved:
  unrelated dirt outside both the committed and staged deltas still skips
  the finalize and reports `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`; an
  unavailable anchor (`operation_start_head is None`) and an unresolvable
  delta (either diff command fails) still skip and stay fail-closed (the
  helper short-circuits on the first failed diff, so the staged-diff command
  never runs when the committed-diff fails).

## Requirement-by-requirement status
- [x] Regression test: staged-only-dirt (empty committed delta, non-empty
      staged delta) is finalized and validation proceeds. TDD red confirmed
      against the unfixed code (`unrelated_dirty=['src/fix.py']` log, assert
      `passed is True` failed); green after the fix.
- [x] Existing unrelated-dirt fail-closed test kept green (now exercises both
      an empty committed and an empty staged delta via the default
      `FakeCommandRunner` empty-result fallback; the docstring was updated
      to reference the staged-delta union).
- [x] Minimal fix in `_operation_owned_delta_paths`: union the committed
      delta with the staged delta against `operation_start_head`; return
      `None` if either diff fails.
- [x] Existing finalize tests (happy finalize, no-op recheck, policy/ownership/
      protected-scope/provider-retry reason-code preservation, no-anchor
      fail-closed, delta-unavailable fail-closed) still pass; the two
      happy-finalize tests were updated to queue the extra staged-diff
      result the helper now runs.
- [x] Targeted lint + format + typecheck clean on touched files.

## Evidence (files changed + commands run)
- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`:
  `_operation_owned_delta_paths` now runs both
  `git diff --name-only operation_start_head..HEAD` and
  `git diff --name-only --cached operation_start_head`, unions the results,
  and returns `None` when either fails. `_try_finalize_pre_push_dirty_repair_state`
  docstring updated to describe the staged-delta union.
- `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`:
  added `test_pre_push_validation_finalize_commits_operation_owned_staged_dirt`;
  updated the two happy-finalize tests and the unrelated-dirt test docstring.
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q`
  -> 35 passed
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_mixed_127.py tests/unit/runtime/test_pr_monitor_pre_push_validation_cleanup.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/ tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs_validated_push.py -q`
  -> 62 passed
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_runner_parts/ tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/ -q`
  -> 437 passed
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
  -> All checks passed
- `uv run --python 3.12 --extra dev ruff format --check <same files>`
  -> Passed (formatted)
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
  -> Success: no issues found in 1 source file

## Notes
- Full AWF/GitHub broad validation (full coverage gate, whole-repo suites,
  frontend builds) is managed by AWF after agent completion per the
  workspace contract; only the focused checks above were run in-agent.
- No gaps remain against the plan; no iteration required.
