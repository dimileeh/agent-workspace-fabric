# PRRT_kwDOSJAM6s6KaAWk pre-push dirty finalize diff-path normalization validation

Plan reference: `plans/PRRT_kwDOSJAM6s6KaAWk_PLAN.md`

## Requirement-by-requirement status

### [Complete] Rename-source regression test (TDD red -> green)
Added
`test_pre_push_validation_finalize_commits_operation_owned_rename_source_dirt`
(`tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`). It
queues a porcelain dirty check carrying both `oldname.txt` and `newname.txt`
and a staged `--name-status -z` rename record
(`R100\0oldname.txt\0newname.txt\0`), then asserts the finalize commits and
validation proceeds. Confirmed red on the unfixed code (the owned set omitted
`oldname.txt`, so the dirty set minus the owned set treated the rename source
as unrelated dirt -> `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`) and green on
the fixed code.

### [Complete] Non-ASCII path regression test (TDD red -> green)
Added
`test_pre_push_validation_finalize_commits_operation_owned_non_ascii_dirt`
(same file). It queues a porcelain dirty check carrying the decoded
`caf\u00e9.txt` and a staged `--name-status -z` record with the raw UTF-8
path (`M\0caf\u00e9.txt\0`), then asserts the finalize commits and
validation proceeds. Confirmed red on the unfixed code (the raw
`--name-only` line was C-quoted and never matched the decoded dirty path) and
green on the fixed code.

### [Complete] Unrelated-dirt fail-closed test kept green
`test_pre_push_validation_finalize_skips_unrelated_dirt_outside_operation_delta`
still passes: a dirty path outside both the committed and staged operation
deltas fails as `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`. Its docstring was
updated to reference `--name-status -z --cached operation_start_head`.

### [Complete] Delta-unavailable fail-closed test kept green
`test_pre_push_validation_finalize_skips_when_operation_delta_unavailable`
still passes: when either `git diff` fails the helper returns `None` and the
finalize is skipped. Its docstring was updated to reference
`git diff --name-status -z operation_start_head..HEAD`.

### [Complete] Malformed-delta fail-closed regression test added
Added
`test_pre_push_validation_finalize_skips_when_operation_delta_malformed`
(same file). It queues a committed `--name-status -z` git command that
succeeds but returns malformed output (`M\tsrc/fix.py\n`, no NUL delimiter),
asserts the parser raises `ProtectedScopeDiffError`, the helper returns
`None`, the finalize is skipped, and the push fails-closed as
`VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`. This covers the new
`except ProtectedScopeDiffError` branch in `_operation_owned_delta_paths`.

### [Complete] Unowned-post-commit-delta fail-closed test kept green
`test_pre_push_validation_finalize_fail_closed_when_commit_introduces_unowned_paths`
still passes: the post-commit re-validation fails closed with
`PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA` when the committed delta gains an
extra unowned path. Its queued delta outputs were updated from
`--name-only` lines to `--name-status -z` records
(`_name_status_z("M\0src/fix.py\0", "M\0unrelated/extra.py\0")`).

### [Complete] Minimal fix in `_operation_owned_delta_paths`
`src/awf/runtime/pr_monitor_runner/pre_push_validation.py`: both diff
invocations switched from `git diff --name-only ...` to
`git diff --name-status -z ...`, parsed with the existing
`_changed_paths_from_name_status_z` (imported from
`awf.runtime.pr_monitor_runner.path_parsing`). On git failure *or*
`ProtectedScopeDiffError` (malformed NUL-delimited output) the helper returns
`None`, preserving the existing fail-closed delta-unavailable behavior.

### [Complete] Docstrings updated
`_operation_owned_delta_paths` and `_try_finalize_pre_push_dirty_repair_state`
docstrings now describe `--name-status -z` and the rename/non-ASCII
normalization rationale (review thread `PRRT_kwDOSJAM6s6KaAWk`).

### [Complete] Existing finalize tests updated for the new diff form
The finalize tests that previously fed `--name-only`-shaped stdout to the
queued `git diff` results were updated to feed `--name-status -z`-shaped
stdout via a new local `_name_status_z` helper, so they exercise the
intended path (finalize proceeds / fails closed) instead of accidentally
hitting the new malformed-output branch:
- `test_validated_push_finalizes_monitor_dirty_state_before_validation`
- `test_pre_push_validation_finalize_commits_operation_owned_staged_dirt`
- `test_pre_push_validation_rechecks_tree_after_no_op_finalize`
- `test_pre_push_validation_finalize_preserves_policy_blocked_reason_code`
- `test_pre_push_validation_finalize_preserves_ownership_repair_reason_code`
- `test_pre_push_validation_finalize_preserves_protected_scope_diff_unavailable_reason_code`
- `test_pre_push_validation_finalize_threads_remote_branch_and_url_to_commit_sink`
- `test_pre_push_validation_finalize_propagates_provider_recovery_retry`
- `test_pre_push_validation_finalize_propagates_provider_recovery_fallback`
- `test_pre_push_validation_finalize_propagates_provider_recovery_auth`
- `test_pre_push_validation_finalize_fail_closed_when_commit_introduces_unowned_paths`

### [Complete] Targeted lint + typecheck on touched files
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py` -> All checks passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py` -> Success: no issues found in 1 source file.

## Evidence (focused commands — broad validation owned by AWF/GitHub)

Files changed:
- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
  - Imported `_changed_paths_from_name_status_z`.
  - Rewrote `_operation_owned_delta_paths` to run
    `git diff --name-status -z operation_start_head..HEAD` and
    `git diff --name-status -z --cached operation_start_head`, parse each
    with `_changed_paths_from_name_status_z`, and return `None` on git
    failure or `ProtectedScopeDiffError`.
  - Updated `_operation_owned_delta_paths` and
    `_try_finalize_pre_push_dirty_repair_state` docstrings.
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py`
  - Added `_name_status_z` helper.
  - Added 3 regression tests (rename source, non-ASCII, malformed delta).
  - Updated 11 existing finalize tests to feed `--name-status -z`-shaped
    stdout and refreshed docstrings.

Focused verification (run during this task):
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py -q` -> 21 passed.
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/control/test_protected_file_diffs.py -q` -> 19 passed.
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs_validated_push.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py tests/unit/runtime/test_pr_monitor_pre_push_validation_cleanup.py tests/unit/runtime/test_pr_monitor_pre_push_validation_mixed_127.py -q` -> 37 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py` -> All checks passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py` -> Success.

Full AWF/GitHub broad validation (coverage gate, whole-repo suites, full
frontend builds) is managed by AWF after agent completion and was not run
here, per the AWF workspace contract.

## Pass criteria status
- New rename-source and non-ASCII regression tests fail on the unfixed code
  and pass on the fixed code: Complete.
- Existing finalize tests (finalize, no-op recheck, unrelated-dirt
  fail-closed, no-anchor fail-closed, delta-unavailable fail-closed,
  malformed-delta fail-closed, policy/ownership/protected-scope/provider
  reason-code preservation, unowned-post-commit-delta fail-closed) pass:
  Complete.
- Lint/typecheck clean on touched files: Complete.

No gaps remaining; no further iteration required.
