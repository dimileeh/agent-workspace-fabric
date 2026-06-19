# Fix: Track post-validation conformance report rewrite result explicitly

Thread: `PRRT_kwDOSJAM6s6KL7-o`
File: `src/awf/control/executor/planning_ops.py` (line ~393)

## Problem statement

In `_run_post_validation_conformance_check`, when a satisfied report is supplied
only via stdout (or a stale on-disk report), AWF synthesizes a fresh satisfied
report by calling `_write_satisfied_post_validation_conformance_report`. If that
write raises (read-only worktree, disk full, etc.), the code currently decides
which artifact deposit path to take by checking whether the report file exists on
disk:

```python
stdout_report_path = worktree_path / handoff.report_path
write_succeeded = stdout_report_path.is_file()
if not report_from_fresh_file and report_text is not None and not write_succeeded:
    _deposit_satisfied_conformance_report(...)  # in-memory satisfied report
else:
    deposit_workspace_planning_artifacts(...)   # copies the worktree file
```

If the write failed but an older stale handoff report still exists on the
worktree, `is_file()` returns True and the `else` branch copies the stale
worktree file into the served artifacts. The validation event records
satisfaction while the served artifact still shows the previous gaps, creating a
consistency bug.

The code must instead track whether the explicit rewrite call itself succeeded,
rather than inferring success from file presence.

## Requirements checklist

- [ ] `_run_post_validation_conformance_check` records the actual success/failure
      of the AWF-synthesized rewrite in a dedicated boolean flag.
- [ ] The artifact-deposit branch decision uses that flag, not file existence.
- [ ] A regression test exercises the exact bug: stdout-only satisfied report,
      a stale report file already on disk, the AWF rewrite fails, and the served
      artifact still receives the in-memory satisfied report (not the stale disk
      copy).
- [ ] Existing regression tests for write-failure, fresh-file, untracked, and
      tracked-report paths continue to pass.
- [ ] Coverage is preserved or improved; any genuinely unreachable defensive
      branch is justified with a pragma.
- [ ] Focused test suite passes and lint/type checks for the touched files.

## Implementation steps

1. In `src/awf/control/executor/planning_ops.py`, introduce a
   `rewrite_succeeded` flag inside `_run_post_validation_conformance_check`:
   - Initialize to `report_from_fresh_file` (fresh files do not need rewriting).
   - Inside the `if not report_from_fresh_file:` block, set the flag to True
     after `_write_satisfied_post_validation_conformance_report` returns, and
     leave it False if the call raises.
2. Replace the `stdout_report_path.is_file()` check with the explicit flag.
   - `if not report_from_fresh_file and report_text is not None and not rewrite_succeeded:`
     still routes to `_deposit_satisfied_conformance_report`.
   - `else:` routes to `deposit_workspace_planning_artifacts`, which is correct
     when either the file is fresh or the rewrite succeeded (worktree matches
     the in-memory report), and also handles the planning profile not-required
     case (artifact deposit is best-effort and gated later in callers).
3. Update inline comments to document that we now track the actual rewrite call
   rather than inferring from file presence.
4. Add a regression test in `tests/unit/control/test_planning_ops_branch_edges.py`
   that:
   - Creates a stale unsatisfied report on the worktree.
   - Supplies a satisfied report only via stdout.
   - Mocks `_write_satisfied_post_validation_conformance_report` to raise.
   - Asserts that the served artifact dir contains the satisfied report from
     memory (not the stale disk content).
5. Verify the existing `test_satisfied_post_validation_conformance_report_write_failure_proceeds`
   still passes; adjust expectations if it implicitly relied on the old file-existence
   heuristic.
6. Run focused tests and lint/type checks for the touched files.

## Verification commands and pass criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_ops_branch_edges.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py -q
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_ops.py tests/unit/control/test_planning_ops_branch_edges.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py
uv run --python 3.12 --extra dev mypy src/awf/control/executor/planning_ops.py
```

Pass criteria: all four commands succeed without errors.

## Out of scope

- Refactoring other executor paths.
- Changing how `_write_satisfied_post_validation_conformance_report` formats
  or validates the report.
- Adding full-coverage gates; follow the existing test-first focused coverage
  discipline.
