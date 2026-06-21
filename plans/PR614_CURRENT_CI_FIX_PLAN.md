# PR614 Current CI Fix Plan

## Problem Statement and Scope

PR #614 has failing GitHub Actions CI. The latest completed failed run inspected
locally is `27831204526` on commit `1d7110afa95a926a7af8c05001c831efa497fd42`.
Its root failure was `python-coverage-shards (6)`, where
`tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py::test_pre_push_validation_recovered_head_rename_includes_source_path`
expected `PROTECTED_SCOPE_REPAIR_FAILED` but received
`VALIDATION_WORKTREE_STATUS_FAILED`.

Current local HEAD is newer than that failed run, so first confirm whether the
focused failure still reproduces before changing code.

## Requirements Checklist

- [ ] Do not switch branches, push, rebase, or run broad AWF/GitHub-owned
      validation.
- [ ] Use GitHub Actions logs to identify the failing check and concrete root
      cause.
- [ ] Run a focused local repro for the failing test on current HEAD.
- [ ] If the failure still reproduces, make the smallest behavior-preserving
      fix with a regression test.
- [ ] Re-run only focused tests that cover the failing behavior.
- [ ] Record focused evidence in a validation document and note that full
      AWF/GitHub validation remains owned by AWF after agent completion.
- [ ] Commit the local fix or, if no code change is needed because current HEAD
      already fixes the failure, commit only the required plan/validation
      evidence.

## Implementation Steps

1. Inspect PR #614 checks and logs with `gh`, focusing on the latest completed
   failed run and the current in-progress run status.
2. Read the failing test and the relevant pre-push validation recovery path.
3. Run the single failing test on current HEAD.
4. If still failing, adjust the protected-scope recovered-head path so
   recovered committed protected violations return the protected-scope reason
   before validation worktree status handling.
5. Update or add focused tests only around the observed behavior.
6. Create `plans/PR614_CURRENT_CI_FIX_VALIDATION.md` with requirement status and
   command evidence.
7. Commit the scoped changes with a conventional `fix(ci): ...` message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py::test_pre_push_validation_recovered_head_rename_includes_source_path -q`
  must pass.
- If code changes touch neighboring recovery behavior, run the smallest
  neighboring subset needed to cover that behavior.
- Do not run full coverage, all unit tests, or frontend builds locally.
