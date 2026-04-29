# Plan: Parallel Candidate Stale Refresh Regression Slice

## Goal

Add a focused P0 integration/regression test slice proving AWF's merge safety
for parallel PR candidates:

1. Two candidates are created for the same repo/base.
2. The older candidate is initially the merge-queue blocker for the later one.
3. The older candidate lands first and advances the target branch.
4. The later candidate is refreshed, receives a structured stale reason, loses
   merge readiness, and requires recovery.
5. After simulated rebase/refresh plus fresh validation, the stale reason is
   resolved and merge readiness is restored.
6. A companion lenient-policy assertion proves non-overlapping docs/test changes
   stay mergeable when the task-class policy allows it.

Use in-memory SQLite, real AWF repositories/services, and fake target branch
state. Do not use real GitHub, Docker, or branch operations. Production code
changes are only allowed if the red test reveals an actual behavior gap.

## Current Code Context

- `src/awf/service/staleness.py`
  - `evaluate_staleness()` implements pure stale policy.
  - `StalenessRefreshService.refresh_candidate()` persists `stale_reasons`,
    toggles `MergeCandidate.stale`, and calls `sync_candidate_readiness()`.
  - Candidate loading currently controls which workspace relationships are
    available to validation freshness checks.
- `src/awf/service/merge_queue.py`
  - `list_merge_queue_blockers_for_candidate()` reports older open canonical
    candidates for same repo/base when the target candidate is otherwise ready.
  - Monitor-owned recovery operations can keep an older candidate as a queue
    blocker.
- `src/awf/db/repositories.py`
  - `MergeCandidateRepository.create_or_update_open_for_attempt()` creates
    candidate rows and syncs readiness.
  - `StaleReasonRepository` preserves active/resolved stale reason history.
  - `ValidationRunRepository` stores validation provenance.
  - `sync_candidate_readiness()` derives `ready`, `stale`, and
    `stale_reason`.
- `src/awf/runtime/merge_eligibility.py`
  - `stale_reason_required_action()` maps blocking stale reasons to recovery
    actions.
  - `compute_stale_reason_for_attempt()` should require post-rebase Tier 2
    validation for the candidate attempt.
- Existing test files cover many pieces separately:
  - `tests/unit/service/test_staleness.py`
  - `tests/unit/service/test_merge_queue_ordering.py`
  - `tests/unit/service/test_merge_candidates.py`
  - `tests/unit/runtime/test_merge_eligibility.py`
  - `tests/unit/api/test_merge_queue.py`

One validation command requested by the task names
`tests/unit/service/test_merge_queue.py`, which does not currently exist. The
implementation phase should add that file as a focused service regression shim
instead of renaming the existing `test_merge_queue_ordering.py`.

## Intended Files And Modules To Touch

Primary test files:

| File | Intended changes |
| --- | --- |
| `tests/integration/test_parallel_candidate_stale_refresh.py` | New in-memory integration slice. Seeds real workspaces/tasks/attempts/candidates, uses `StalenessRefreshService`, `StaleReasonRepository`, `ValidationRunRepository`, `MergeCandidateRepository`, and `list_merge_queue_blockers_for_candidate()`. |
| `tests/unit/service/test_merge_queue.py` | New narrow regression file so the requested validation command has a real target. Keep it small: assert stale candidates are not ready/blocking and monitor-owned recovery remains visible as a blocker for later ready candidates if that behavior is not already covered by the integration test. |

Production files to touch only if the red tests expose a real gap:

| File | Possible minimal fix |
| --- | --- |
| `src/awf/service/staleness.py` | Load the workspace relationships needed by readiness/freshness recomputation, likely `Workspace.operations` and `Workspace.validation_runs`, before calling `compute_stale_reason()` / `sync_candidate_readiness()`. Preserve existing stale reason replacement semantics. |
| `src/awf/db/repositories.py` | Adjust `sync_candidate_readiness()` only if it cannot correctly preserve blocking stale reasons while allowing validation freshness to clear after successful post-rebase validation. |
| `src/awf/runtime/merge_eligibility.py` | Change only if the test proves the recovery action or post-rebase validation requirement is wrong. Do not weaken tier requirements. |
| `src/awf/service/merge_queue.py` | Change only if queue blocker/readiness state is stale after candidate recovery. Avoid broad queue policy changes. |

Files deliberately not touched:

- Real GitHub / Docker integration code.
- Migrations and database schema.
- `.awf/workspace.yml`, `pyproject.toml`, lockfiles, coverage thresholds, or
  quality-gate config.
- Existing docs other than this plan file.

## Tests To Write First

### 1. Integration: stale refresh and recovery restores readiness

File: `tests/integration/test_parallel_candidate_stale_refresh.py`

Test name:
`test_parallel_candidate_stale_after_older_merge_requires_rebase_then_fresh_validation`

Arrange:

- Use an in-memory SQLite engine and `Base.metadata.create_all`.
- Seed two workspaces on the same repo/base with `auto_merge=True`, PR metadata,
  canonical attempts, and open merge candidates:
  - Candidate A: older, same repo/base, initially ready.
  - Candidate B: later, `task_class="test_task"`, owns a test path such as
    `tests/integration/shared_fixture_test.py`, initially ready with Tier 1
    validation provenance.
- Set deterministic `created_at` values so Candidate A is older.

Assert initial queue state:

- `list_merge_queue_blockers_for_candidate(candidate_b.id)` returns Candidate A.
- Candidate B has `ready=True`, `stale=False`, no active stale reasons.

Act: simulate Candidate A landing first:

- Mark Candidate A merged through `MergeCandidateRepository.mark_workspace_merged()`.
- Refresh Candidate B against fake target state:
  - `head_sha = "b" * 40`
  - `advanced_commits = 1`
  - `changed_paths = ("tests/integration/shared_fixture_test.py",)`

Assert stale state:

- Candidate A no longer blocks Candidate B.
- Candidate B has `ready=False`, `stale=True`.
- `StaleReasonRepository.list_active_for_candidate(candidate_b.id)` includes one
  blocking `STALE_OVERLAP` reason with `trigger_type="path_overlap"`.
- `stale_reason_required_action("STALE_OVERLAP") == "rebase"`.
- The workspace event timeline includes `merge_candidate.stale_detected`.

Act: simulate monitor-owned recovery:

- Create a succeeded `rebase` operation for Candidate B after stale detection,
  with payload `{"source": "pr_monitor", "stale_reason": "STALE_OVERLAP"}`.
- Update Candidate B's merge candidate `base_sha` to the new target head to
  model a successful rebase/refresh onto the advanced branch.
- Refresh Candidate B again with `head_sha == candidate.base_sha`,
  `advanced_commits=0`, and no changed paths.

Assert freshness still blocks until validation:

- Active `STALE_OVERLAP` rows are resolved, not deleted.
- Candidate B remains not ready with
  `stale_reason == "validation_insufficient_tier"` or equivalent validation
  freshness reason.
- This assertion is the likely red-test detector if staleness refresh does not
  load operations/validation runs before recomputing readiness.

Act: restore validation freshness:

- Add a succeeded Tier 2 `ValidationRun` for Candidate B's attempt, started
  after the rebase operation.
- Refresh Candidate B one final time against the same current target state.

Assert recovered state:

- No active blocking stale reasons remain.
- Historical stale reason row has `status="resolved"` and `resolved_at` set.
- Candidate B has `stale=False`, `stale_reason is None`, and `ready=True`.
- `list_merge_queue_blockers_for_candidate(candidate_b.id)` returns `[]`.

### 2. Integration: lenient docs/test non-overlap remains mergeable

File: `tests/integration/test_parallel_candidate_stale_refresh.py`

Test name:
`test_non_overlapping_docs_and_test_target_changes_remain_ready_when_policy_allows`

Arrange:

- Seed a docs candidate with `task_class="docs_task"` and
  `owned_paths=["docs/user-guide.md"]`.
- Seed a test candidate with `task_class="test_task"` and
  `owned_paths=["tests/unit/service/test_status.py"]`.
- Both are same repo/base, open, canonical, auto-merge candidates with Tier 1
  validation.

Act/assert:

- Refresh the docs candidate after target advancement that only changes the
  unrelated test path. Assert no active blocking stale reason and
  `candidate.ready is True`.
- Refresh the test candidate after target advancement that only changes the
  unrelated docs path. Assert no active blocking stale reason and
  `candidate.ready is True`.
- If plan-artifact paths under `docs/awf-plans/**` are included in the target
  changes, assert the advisory reason does not set `candidate.stale`.

### 3. Service shim: merge queue readiness target

File: `tests/unit/service/test_merge_queue.py`

Test name:
`test_stale_candidate_drops_out_of_merge_ready_queue_blocker_lookup`

- Seed two same repo/base candidates with the existing helper pattern from
  `test_merge_queue_ordering.py`.
- Mark the later candidate stale and sync readiness.
- Assert the later candidate is not merge-ready and
  `list_merge_queue_blockers_for_candidate(later.id)` returns `[]` because
  stale readiness is the blocker, not the older-candidate queue check.

Optional second test if the integration test does not cover this path:
`test_monitor_owned_recovery_candidate_blocks_later_ready_candidate`

- Older candidate is in `ready` or `validating` with an active
  `Operation(type="rebase" or "validate", status="pending" or "running",
  payload={"source": "pr_monitor"})`.
- Later candidate is otherwise ready.
- Assert the blocker state is `monitor_owned_recovery`.

## Implementation Steps After Red Tests

1. Add the integration test file and service shim tests first.
2. Run only the new focused tests and confirm any failure is for the intended
   contract, not fixture setup.
3. If stale reason creation, resolution, or queue blocker behavior already
   passes, keep production untouched for those paths.
4. If post-rebase validation freshness clears too early, minimally update
   `StalenessRefreshService` candidate loading so readiness recomputation sees
   the workspace's operations and validation runs.
5. If readiness flags fail to update after successful Tier 2 validation, fix
   the narrow sync path rather than changing stale-policy rules.
6. Re-run the targeted tests, then the requested validation commands.

## Validation Commands

Use the task-requested commands exactly:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests/unit tests/integration
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/integration tests/unit/service/test_staleness.py tests/unit/service/test_merge_queue.py -q
uv run --python 3.12 --extra dev pytest tests/unit -q
```

Additional focused loop during implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/integration/test_parallel_candidate_stale_refresh.py tests/unit/service/test_merge_queue.py -q
```

## Risks And Assumptions

- Assumption: An in-memory SQLite integration test is sufficient because this
  slice validates control-plane state transitions, not GitHub or Docker
  mechanics.
- Assumption: The production recovery path records successful rebase and
  validation as `Operation` / `ValidationRun` rows; the integration test can
  simulate those rows directly without invoking the executor.
- Assumption: `test_task` is the right stale-recovery probe because Tier 1 is
  enough initially, but a successful rebase should raise the freshness
  requirement to Tier 2.
- Risk: Existing `compute_stale_reason()` defaults may treat an unloaded or
  absent validation history as Tier 1. The test should expose that only in the
  post-rebase case; any fix must be narrow and must not break existing
  candidate creation behavior.
- Risk: Timestamp ordering matters for post-rebase validation. The test should
  set deterministic `created_at` / `started_at` values instead of relying on
  wall-clock ordering.
- Risk: Adding `tests/unit/service/test_merge_queue.py` creates overlap with
  `test_merge_queue_ordering.py`. Keep the new file limited to the path named
  by the requested validation command.

## Explicit Non-Goals

- Do not build a real merge queue runner or end-to-end GitHub PR workflow.
- Do not invoke Docker, `gh`, live git remotes, branch switching, push, or
  rebase commands.
- Do not introduce new schema, migrations, task states, or queue policy.
- Do not lower coverage or validation thresholds.
- Do not broaden advisory owned-path policy into admission blocking.
- Do not refactor PR monitor or executor recovery behavior unless the new
  integration test demonstrates that their persisted state contract is wrong.
- Do not commit during this planning phase.
