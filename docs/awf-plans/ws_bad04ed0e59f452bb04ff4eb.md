# Plan: P0 Owned-Path Overlap Stale Detection

## Objective

Implement the P0 stale-detection slice where owned-path overlap is advisory at
workspace launch, then becomes a structured stale-risk input after another PR
lands on the target branch. Overlap must not prevent workspace creation or
submission. When a target-branch merge changes a path claimed by another open
merge candidate, AWF should persist an active stale reason for the affected
candidate/workspace and expose it through the existing stale reason and merge
queue APIs. Non-overlapping target changes must not create that owned-path
overlap reason.

## Current Code Context

- **Advisory admission**: `src/awf/service/workspaces.py`
  - `create_workspace_v2_row()` already computes active owned-path overlaps,
    records `QueueDecision.overlap_risk_summary`, and emits
    `workspace.owned_path_overlap_risk`.
  - This is the launch-time path that must remain advisory.

- **Owned-path matching**: `src/awf/db/repositories.py`
  - `WorkspaceRepository.find_active_owned_path_overlaps()` and helper matchers
    are used for admission-time overlap metadata.

- **Stale detection engine**: `src/awf/service/staleness.py`
  - `evaluate_staleness()` turns `CandidateSnapshot` plus
    `TargetBranchState.changed_paths` into structured `StalenessFinding` rows.
  - `StalenessRefreshService.refresh_candidate()` persists active rows via
    `StaleReasonRepository`, marks `MergeCandidate.stale`, and emits
    `merge_candidate.stale_detected` events.
  - Existing reason shape should be reused: `STALE_OVERLAP` with
    `trigger_type="path_overlap"` and `trigger_ref` set to the changed target
    path.

- **Post-merge target refresh**: `src/awf/service/target_branch_monitor.py`
  - `reconcile_and_refresh_stale_candidates()` refreshes all still-open
    candidates for the same repo/base branch after target reconciliation,
    excluding the workspace that just merged.

- **API exposure**:
  - `src/awf/api/routes/merge_queue.py` loads active `stale_reasons` for queue
    items and uses blocking stale reasons to set `merge_blocker_reason`.
  - `src/awf/api/routes/workspaces.py` exposes
    `/v1/workspaces/{workspace_id}/stale-reasons`.
  - `src/awf/api/schemas.py` defines the existing response fields and literals.

## Intended Files And Modules To Touch

### Production Code

- `src/awf/service/staleness.py`
  - Ensure owned-path overlap detection is policy-consistent, deterministic, and
    based on the open candidate/workspace owned paths versus target branch
    `changed_paths`.
  - Preserve advisory handling for AWF plan artifacts if already present, but
    keep source owned-path overlap blocking when policy requires refresh/rebase.
  - If needed, normalize path matching edge cases so `src/foo/**` and
    `src/foo/bar.py` match consistently.

- `src/awf/service/target_branch_monitor.py`
  - Ensure post-merge reconciliation refreshes every open candidate on the same
    repo/base branch, skips the just-merged workspace, and carries
    `STALE_OVERLAP` summaries for affected candidates.
  - Keep individual refresh failures isolated.

- `src/awf/db/models.py`
  - Only touch if the existing stale reason literal/severity helpers need an
    additional advisory/blocking classification. Prefer no schema change.

- `src/awf/api/schemas.py`
  - Only touch if a new reason code or trigger literal is unavoidable. Prefer
    existing `STALE_OVERLAP` / `path_overlap` to avoid response-shape churn.

- `src/awf/api/routes/merge_queue.py`
  - Only touch if current readiness/blocker projection does not surface active
    overlap stale reasons correctly.

### Tests

- `tests/unit/service/test_workspace_owned_path_policy.py`
  - Add or tighten launch/submission tests proving overlapping owned paths are
    admitted and produce advisory risk metadata, not a hard conflict.

- `tests/unit/service/test_staleness.py`
  - Add pure stale-evaluation regressions for overlapping and non-overlapping
    target changed paths.

- `tests/unit/service/test_target_branch_monitor.py`
  - Add post-merge integration-style unit tests around
    `reconcile_and_refresh_stale_candidates()`.

- `tests/unit/api/test_stale_reasons.py`
  - Add stale-reason endpoint coverage for the owned-path overlap reason.

- `tests/unit/api/test_merge_queue.py`
  - Add merge queue coverage proving the existing response shape includes the
    active `STALE_OVERLAP` reason and stale readiness/blocker fields.

## Tests To Write First

1. **`test_create_v2_overlap_is_advisory_for_merge_candidate_workspaces`** in
   `tests/unit/service/test_workspace_owned_path_policy.py`
   - Seed an active workspace with `owned_paths=["src/awf/service/**"]`.
   - Create a second workspace with
     `owned_paths=["src/awf/service/staleness.py"]`.
   - Assert creation succeeds, status remains requested, the second workspace
     retains its owned paths, and overlap appears only in the warning event /
     queue decision metadata.

2. **`test_owned_path_target_change_emits_structured_overlap_reason`** in
   `tests/unit/service/test_staleness.py`
   - Call `evaluate_staleness()` with a candidate owning
     `src/awf/service/**` and a target changed path
     `src/awf/service/staleness.py`.
   - Assert one `STALE_OVERLAP` finding with
     `trigger_type="path_overlap"`, `trigger_ref` set to the changed path,
     `severity="blocking"`, and `blocks_merge=True`.

3. **`test_non_overlapping_target_change_does_not_emit_overlap_reason`** in
   `tests/unit/service/test_staleness.py`
   - Use a lenient candidate class such as `docs_task` with owned path
     `docs/**` and target changed path `src/awf/service/staleness.py`.
   - Assert no `STALE_OVERLAP` finding and no stale findings for that policy
     case.

4. **`test_reconcile_after_merge_marks_only_overlapping_open_candidate_stale`** in
   `tests/unit/service/test_target_branch_monitor.py`
   - Seed two open candidates on the same repo/base: one owns
     `src/awf/service/**`, the other owns `docs/**`.
   - Run `reconcile_and_refresh_stale_candidates()` with target
     `changed_paths=("src/awf/service/staleness.py",)` and
     `exclude_workspace_ids` containing the just-merged workspace.
   - Assert only the service candidate gets an active `STALE_OVERLAP` row,
     `MergeCandidate.stale=True`, a summary stale reason of `STALE_OVERLAP`,
     and a `merge_candidate.stale_detected` event.

5. **`test_reconcile_after_merge_non_overlap_leaves_candidate_without_overlap_reason`**
   in `tests/unit/service/test_target_branch_monitor.py`
   - Seed an open candidate owning `docs/**`.
   - Refresh against target changed path `src/awf/service/staleness.py`.
   - Assert no active `STALE_OVERLAP` stale reason and no overlap stale event.

6. **`test_workspace_stale_reasons_endpoint_exposes_owned_path_overlap_reason`**
   in `tests/unit/api/test_stale_reasons.py`
   - After a refresh that creates `STALE_OVERLAP`, call
     `/v1/workspaces/{workspace_id}/stale-reasons`.
   - Assert the existing item shape includes `reason_code`, `trigger_type`,
     `trigger_ref`, `severity`, `blocks_merge`, timestamps, and active status.

7. **`test_merge_queue_exposes_owned_path_overlap_stale_reason_without_shape_break`**
   in `tests/unit/api/test_merge_queue.py`
   - After a refresh that creates `STALE_OVERLAP`, call `/v1/merge-queue`.
   - Assert `readiness.stale=True`, `readiness.stale_reason` remains compatible
     with existing behavior, `merge_blocker_reason="stale"`,
     `required_next_action="rebase"`, and `stale_reasons[0]` has the existing
     response keys with `reason_code="STALE_OVERLAP"`.

## Implementation Steps

1. Run the focused tests above after adding them and confirm they fail for the
   intended missing behavior before production changes.

2. Tighten `evaluate_staleness()` if necessary so changed target paths are
   matched against candidate owned paths using the same semantics as advisory
   admission: exact path, directory prefix, `/**`, `/*`, and bounded glob
   patterns.

3. Ensure the overlap finding uses the existing structured stale reason:
   `reason_code="STALE_OVERLAP"`, `trigger_type="path_overlap"`,
   `trigger_ref=<changed target path>`, blocking severity, and an explanation
   that names the target branch.

4. Verify `StalenessRefreshService.refresh_candidate()` persists the overlap
   finding through `StaleReasonRepository.replace_active_findings()`, marks the
   candidate stale, syncs derived readiness, and emits
   `merge_candidate.stale_detected` only for newly added reasons.

5. Verify `reconcile_and_refresh_stale_candidates()` applies refreshes only to
   still-open candidates on the same repo/base branch and excludes the
   just-merged workspace. If summary output loses the overlap code, preserve it
   via `_summary_stale_reason()`.

6. Keep launch-time owned-path overlap behavior unchanged: workspace creation,
   retry, and queue admission continue to write advisory risk metadata without
   returning errors or blocking submission.

7. Confirm API routes already expose the active stale reason through
   `/v1/merge-queue` and `/v1/workspaces/{id}/stale-reasons`; adjust only the
   minimal schema literal or projection code if a new code path needs it.

## Validation Commands

Focused TDD commands:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_owned_path_policy.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_staleness.py tests/unit/service/test_target_branch_monitor.py -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_stale_reasons.py tests/unit/api/test_merge_queue.py -q
```

Required full validation for this task:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service tests/unit/api tests/unit/runtime -q
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

Coverage guard if changes touch shared stale-detection or merge-queue behavior
broadly:

```bash
uv run --python 3.12 --extra dev pytest --cov=awf --cov-report=term-missing
```

## Risks And Assumptions

- **Assumption**: `STALE_OVERLAP` is the correct structured reason code for
  post-merge owned-path overlap. It already maps to blocking merge behavior via
  existing stale reason helpers.
- **Assumption**: A target-branch merge is represented to stale detection as
  `TargetBranchState.changed_paths` since the candidate `base_sha`; no extra
  PR-level changed-path table is needed for this slice.
- **Assumption**: Existing `stale_reasons` and `workspace_events` tables are
  sufficient; no migration should be required.
- **Risk**: Broad target advancement can also create `STALE_TARGET_ADVANCED`
  for non-lenient task classes. Tests should isolate owned-path overlap with
  lenient task classes where needed, or assert the overlap reason specifically
  when multiple findings are expected.
- **Risk**: Matching semantics can diverge between advisory launch overlap and
  stale detection. Keep matcher behavior explicit in tests and avoid ad hoc
  string comparisons.
- **Risk**: API response changes can break console clients. Prefer adding no
  fields and reusing existing `stale_reasons[]`, `readiness`, and
  `merge_blocker_reason` fields.
- **Risk**: Post-merge refresh must not mark the just-merged workspace stale;
  keep `exclude_workspace_ids` behavior covered.

## Explicit Non-Goals

- Do not make owned paths exclusive locks or admission blockers.
- Do not serialize `migration_task`, `dependency_task`, or `build_config_task`
  workspaces via owned-path overlap.
- Do not add a new database table or migration unless existing stale reason
  storage is demonstrably insufficient.
- Do not change merge automation, PR monitor grace timing, or manual merge
  behavior.
- Do not implement future exclusive resource locks.
- Do not add console UI changes in this slice.
- Do not lower coverage thresholds, quality gates, or `.awf/workspace.yml`
  requirements.
- Do not push, rebase, switch branches, manually merge PRs, or create commits
  during this planning phase.
