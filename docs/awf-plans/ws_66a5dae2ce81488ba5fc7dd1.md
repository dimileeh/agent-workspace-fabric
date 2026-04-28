# Plan: Configurable Non-Check Reviewer Settle Policy

## Goal

Harden the PR monitor against configured async reviewers that can post late
review comments without publishing GitHub-visible checks or commit statuses.
The new wait must be independent from the existing initial review grace and
pre-merge settle windows, must apply only to auto-merge PRs that are otherwise
merge-ready, and must restart per PR head SHA.

## Intended Files And Modules

- `src/awf/runtime/pr_monitor.py`
  - Add `MonitorConfig.non_check_reviewer_settle_seconds`, default `180.0`.
  - Add `MonitorConfig.non_check_reviewer_logins`, defaulting to a narrow
    Greptile-only identity set if tests prove matching is safe.
  - Keep `decide()` behavior unchanged for comments, CI, mergeability, and
    manual merge mode unless a new pure action is proven necessary.

- `src/awf/runtime/pr_monitor_runner.py`
  - Add pure helper functions for:
    - normalizing configured reviewer identities;
    - detecting whether configured reviewers are represented by current-head
      GitHub-visible check/status data;
    - computing the PR-number plus head-SHA settle wait using
      `MonitorState.threads_addressed_ids`.
  - Insert the wait in the `Merge` execution path only after existing AWF merge
    gates, scope policy refresh, and merge queue checks pass, and before the
    existing merge critical section / `pre_merge_settle_seconds` recheck.
  - Persist state keys in `threads_addressed_ids`, for example:
    - `__awf_non_check_reviewer_settle_started__:{pr_number}:{head_sha}`
    - `__awf_non_check_reviewer_settle_done__:{pr_number}:{head_sha}`
  - Emit structured `_log` entries and `monitor.log` JSON events for start,
    still waiting, elapsed, and visible-check skip. Add workspace events for
    durable start/elapsed/skip transitions if this matches existing event
    volume patterns; keep per-poll "still waiting" logging lightweight.

- `src/awf/runtime/release_pr_monitor.py`
  - Thread the new config fields through `build_feature_pr_monitor()` and
    `build_release_pr_monitor()`. Release/manual monitors still skip the wait
    because `MonitorConfig.auto_merge` is false.

- `src/awf/profiles/models.py`
  - Extend `ProfileMonitor` with:
    - `non_check_reviewer_settle_seconds: float = 180.0`
    - `non_check_reviewer_logins: list[str] = [...]`
  - Keep `extra="forbid"` and add normalization/deduplication if needed.
  - Preserve backward compatibility so old profiles load with defaults.

- `src/awf/service/worker.py`
  - Pass profile monitor values into the PR monitor builders.
  - Keep the existing per-workspace override behavior only for
    `initial_review_grace_period_seconds`; do not add DB fields for the new
    policy unless a test exposes a real need.

- `src/awf/common/github_client.py`
  - Only if needed for reliable identity matching, extend `CheckTiming` and the
    GraphQL query to capture provider identity metadata such as CheckRun app
    slug/name and StatusContext creator login. Keep the existing `name` field
    as a fallback for status context names like `Greptile`.

- `.awf/workspace.yml`
  - In the implementation phase, add a self-profile `monitor:` section with a
    sensible settle value and the narrow Greptile-style login default.

- Tests:
  - `tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py` (new)
  - `tests/unit/runtime/test_pr_monitor_runner.py` or
    `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
  - `tests/unit/runtime/test_pr_monitor_merge_safety.py` if direct `_execute`
    merge-gate coverage fits best there
  - `tests/unit/profiles/test_profiles.py`
  - `tests/unit/service/test_worker.py`
  - `tests/unit/common/test_github_client.py` only if `CheckTiming` parsing is
    extended

## TDD Test Plan

Write and run the failing tests before implementation, in this order:

1. Config defaults
   - `MonitorConfig().non_check_reviewer_settle_seconds == 180`.
   - Default `non_check_reviewer_logins` is a narrow Greptile-style set, not a
     broad "all bots" set.
   - `ProfileMonitor` and `WorkspaceProfile` load old/minimal profiles with
     the same defaults.

2. Profile config loading
   - Explicit `.model_validate({"monitor": {"non_check_reviewer_settle_seconds":
     45, "non_check_reviewer_logins": ["greptile-apps"]}})` loads.
   - `0` seconds is accepted and disables the policy.
   - Empty login lists are accepted as effectively no-op, or rejected only if a
     clear schema reason is documented.

3. Worker and builder wiring
   - `build_worker_runtime()` passes profile-level
     `non_check_reviewer_settle_seconds` and `non_check_reviewer_logins` to the
     feature monitor builder.
   - Release monitor builder receives the same config but the runtime policy
     skips because `auto_merge=False`.

4. Pure visibility and wait helpers
   - With configured Greptile login and no matching current-head check/status,
     an otherwise green auto-merge PR returns a wait of `min(poll_interval,
     remaining)`, records the start key, and does not mark elapsed.
   - With `non_check_reviewer_settle_seconds=0`, helper returns no wait and
     does not mutate state.
   - With `auto_merge=False`, helper returns no wait and does not mutate state.
   - With a visible Greptile check/status on the current head, helper skips the
     extra wait and records/logs a visible-check skip.
   - A completed wait for `head_sha=A` does not satisfy `head_sha=B`; the new
     head starts a fresh wait.
   - Elapsed wait marks the PR/head done and allows the caller to continue to
     the existing merge path.

5. Runner merge-path behavior
   - Direct `_execute(Merge())` with all GitHub-visible gates green, a valid
     merge candidate, auto-merge enabled, configured Greptile login, and no
     visible Greptile check sleeps/polls instead of calling `gh pr merge`.
   - Re-running with the same PR/head before elapsed continues waiting and
     re-fetches on the outer monitor loop.
   - Re-running after elapsed proceeds to the existing merge critical section
     and, if `pre_merge_settle_seconds` is configured, performs the existing
     final settle/recheck before merge.

6. Comments during the wait
   - Full runner loop regression: first poll reaches non-check reviewer wait;
     next poll has a new Greptile review thread/comment. `decide()` must return
     `AddressComments`, the adapter is invoked, and merge is not attempted in
     that iteration.

7. PR #93 regression coverage
   - All GitHub-visible checks/statuses are green, merge state is clean, no
     unresolved comments are present, and Greptile has no visible check/status.
     Auto-merge is blocked until the non-check reviewer quiet window elapses.
   - The same fixture with a visible Greptile check/status skips the extra wait
     and uses ordinary check gating.

## Implementation Approach

1. Add failing tests for config shape and profile loading.
2. Add failing tests for pure helper behavior before touching runner control
   flow.
3. Implement config fields and profile schema defaults.
4. Implement reviewer identity normalization and visible-check detection. Match
   by exact provider identity when available, and by conservative normalized
   check/status context name fallback for Greptile-style names.
5. Implement the settle helper using `MonitorState.threads_addressed_ids`.
   The helper should:
   - no-op when disabled, no configured logins, or `auto_merge=False`;
   - skip only configured reviewers represented by visible checks/statuses;
   - wait when at least one configured reviewer is missing visible
     representation;
   - return poll-sized waits so the outer loop keeps refetching;
   - mark elapsed for the current PR/head and not restart for that same head.
6. Call the helper from the `Merge` action path after the PR is otherwise ready
   under AWF gates and before `pre_merge_settle_seconds`.
7. Add structured monitor logging and durable event coverage with dedupe keys in
   state where needed.
8. Wire profile values through worker and monitor builders.
9. Update `.awf/workspace.yml` with the self-profile monitor setting.
10. Run targeted tests after each implementation slice, then run the broader
    validation commands below.

## Validation Commands

Targeted while developing:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_merge_safety.py -q
uv run --python 3.12 --extra dev pytest tests/unit/profiles/test_profiles.py tests/unit/service/test_worker.py -q
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q
```

Required AWF validation surface:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit -q
```

Coverage when the monitor runner changes are complete:

```bash
uv run --python 3.12 --extra dev pytest --cov=awf --cov-report=term-missing
```

## Risks And Mitigations

- Reviewer identity matching can be too broad. Mitigation: keep defaults narrow,
  normalize conservatively, and test Greptile-specific aliases without treating
  every bot login as a non-check reviewer.
- GitHub check metadata may not expose app login uniformly for checks and
  statuses. Mitigation: prefer provider metadata when available and fall back to
  current check/status context names; document the fallback in tests.
- Adding sleeps in the wrong place could delay stale recovery or manual PRs.
  Mitigation: invoke only inside the auto-merge `Merge` action path after AWF
  gates say the PR is otherwise ready.
- Persisted state key churn could pollute `threads_addressed_ids`. Mitigation:
  use namespaced keys and avoid per-poll unique keys.
- Pre-merge timing could double-wait unexpectedly. Mitigation: keep this policy
  separate from `initial_review_grace_period_seconds` and
  `pre_merge_settle_seconds`; after the non-check reviewer wait elapses, proceed
  to the existing final settle/recheck unchanged.

## Assumptions

- `PRStatus.checks` represents check/status contexts for the current head SHA
  because it is parsed from the last commit in the PR GraphQL response.
- It is acceptable to persist monitor-only bookkeeping in
  `Workspace.monitor_threads_addressed` via `MonitorState.threads_addressed_ids`.
- No DB migration is needed because the new policy is profile-level runtime
  configuration and state is namespaced in existing JSON state.
- Default Greptile-style identities are safe only if tests prove they do not
  expand to arbitrary bot reviewers.

## Explicit Non-Goals

- Do not change GitHub branch protection, required checks, or merge queue
  semantics.
- Do not make async reviewer waits apply to `auto_merge=false` PRs.
- Do not replace existing initial review grace or pre-merge settle behavior.
- Do not add task-level API fields or DB columns for this policy.
- Do not lower coverage thresholds, `.awf` validation gates, or test strictness.
- Do not push, rebase, switch branches, or create a PR manually.
