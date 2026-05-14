# Merge Conflict Test Worker Validation

Plan reference: `MERGE_CONFLICT_TEST_WORKER_PLAN.md`

## Requirement Status

- Preserve the current PR test that verifies preserved active execution events
  keep primary failure evidence: Complete.
- Preserve the base-branch test that verifies expired preserved active
  executions fail and clean up runtime resources: Complete.
- Remove all conflict markers from `tests/unit/control/test_worker.py`:
  Complete.
- Do not stage unrelated unstaged or untracked user changes outside the merge
  resolution: Complete.
- Run a narrow verification command for the touched worker tests: Complete.
- Create a validation document for this plan: Complete.
- Commit the merge resolution locally with a conventional commit message:
  Complete after the merge commit is created.

## Evidence

Files intentionally changed for the conflict resolution:

- `tests/unit/control/test_worker.py`
- `plans/MERGE_CONFLICT_TEST_WORKER_PLAN.md`
- `plans/MERGE_CONFLICT_TEST_WORKER_VALIDATION.md`

Commands run:

- `rg -n "<<<<<<<|=======|>>>>>>>" tests/unit/control/test_worker.py`
  returned no matches.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  passed with `177 passed in 143.15s`.

## Gaps

No gaps remain.
