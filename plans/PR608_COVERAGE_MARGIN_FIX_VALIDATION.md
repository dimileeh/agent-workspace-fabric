# PR608 Coverage Margin Fix Validation

Plan reference: `plans/PR608_COVERAGE_MARGIN_FIX_PLAN.md`

## Requirement Status

- Inspect PR #608 Actions logs and coverage artifact before changing code:
  Complete. `gh run view 27817076948 --log-failed` showed
  `python-full-coverage` failed because combined coverage was `98.97`, below
  `99.00`; `ci-required` failed only as a downstream required-job check. The
  failed run's `full-coverage-report` artifact was downloaded and inspected.
- Add behavior-focused coverage for real planning conformance artifact fallback
  behavior:
  Complete. Current HEAD already had the fallback coverage tests from later
  commits, but a focused current repro found
  `test_post_validation_conformance_staged_deletion_restored_from_head` still
  asserted the old raw report path. The test now asserts the production
  literal pathspec used by `planning_conformance.py`.
- Keep changes scoped:
  Complete. Code changes are limited to one assertion in
  `tests/unit/control/test_planning_ops_branch_edges.py`, plus this plan and
  validation record.
- Run targeted tests only:
  Complete. Full AWF/GitHub validation remains owned by AWF/GitHub after agent
  completion.
- Commit the local fix:
  Complete. The local commit will include the scoped assertion fix plus the
  plan/validation records.

## Evidence

Failure reproduced locally with a focused file run before the fix:

```bash
uv run --python 3.12 --extra dev coverage erase && \
uv run --python 3.12 --extra dev coverage run -m pytest \
  tests/unit/control/test_planning_ops_branch_edges.py -q
```

Observed failure:

- `test_post_validation_conformance_staged_deletion_restored_from_head`
- Expected command text used `-- docs/awf-plans/ws_post.conformance.json`
- Actual production command uses `-- :(literal)docs/awf-plans/ws_post.conformance.json`

Passing focused checks after the fix:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_planning_ops_branch_edges.py::test_post_validation_conformance_staged_deletion_restored_from_head -q
```

Result: `1 passed in 0.71s`

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_planning_ops_branch_edges.py -q
```

Result: `28 passed in 1.10s`

Final post-format focused file check:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_planning_ops_branch_edges.py -q
```

Result: `28 passed in 1.02s`

Targeted lint/format checks:

```bash
uv run --python 3.12 --extra dev ruff check \
  tests/unit/control/test_planning_ops_branch_edges.py
uv run --python 3.12 --extra dev ruff format --check \
  tests/unit/control/test_planning_ops_branch_edges.py
```

Results: `All checks passed!`; `1 file already formatted`

Additional focused conformance fallback checks run before the assertion fix:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_planning_ops_branch_edges.py::test_empty_report_parent_residue_treats_oserror_as_dirty \
  tests/unit/control/test_planning_ops_branch_edges.py::test_remove_stale_satisfied_conformance_artifacts_logs_unlink_oserror \
  tests/unit/control/test_planning_ops_branch_edges.py::test_deposit_satisfied_conformance_report_mkdir_oserror_is_non_fatal \
  tests/unit/control/test_planning_ops_branch_edges.py::test_post_validation_conformance_report_deposit_oserror_is_non_fatal \
  tests/unit/control/test_planning_ops_branch_edges.py::test_deposit_satisfied_conformance_report_rejects_oversized_report -q
```

Result: `5 passed in 0.78s`

## Remaining Validation

The full coverage gate and current PR check rerun are intentionally not run
locally. AWF/GitHub CI owns broad coverage, provenance, logs, and merge gating
after the agent commits and exits.
