# CI Full Coverage Origin Branch Plan

## Problem Statement and Scope

PR #678 now passes the prior Python coverage shard failures, but the current
GitHub Actions run fails `python-full-coverage` at 98.996%, just under the 99%
combined line+branch threshold. The downloaded coverage report identifies
uncovered branches in the merge-block attention origin helper, specifically the
persisted and explicit non-merge-rejection origin paths.

Scope is limited to owned runtime PR monitor tests. Do not change coverage
thresholds, workflow/configuration files, or production behavior unless the
focused tests expose a real implementation defect.

## Requirements Checklist

- [ ] Add meaningful regression tests for merge-block attention origin behavior
      that is currently untested.
- [ ] Cover persisted merge-rejection origin so a restarted monitor preserves
      operator attention through a GitHub `CLEAN` queue wait.
- [ ] Cover persisted non-merge origin so ordinary branch-protection attention
      clears when GitHub reports `CLEAN`.
- [ ] Cover explicit in-memory non-merge origin precedence so stale persisted
      merge-rejection origin cannot preserve an ordinary marker.
- [ ] Run only focused local verification for the changed tests and lint surface.
- [ ] Record validation evidence and leave broad AWF/GitHub validation to the
      post-agent workflow.

## Implementation Steps

1. Add focused tests to the existing merge-attention persistence test module.
2. Assert both in-memory marker state and persisted workspace attention fields
   for preserve and clear outcomes.
3. Run the targeted pytest tests that exercise the new cases.
4. Run a focused lint check for the changed test file and plan/validation docs
   if supported by the repo tooling.
5. Create the validation document and commit the scoped repair locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest <new targeted tests> -q`
  - Passes: the new regression tests succeed against Postgres.
- `uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py`
  - Passes: the changed test file has no lint issues.

Full AWF/GitHub validation, including the exact full coverage gate, is managed
after agent completion and must not be run broadly inside this repair cycle.
