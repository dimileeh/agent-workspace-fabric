# Plan: P0 Provider Resilience — Contract and Regression Coverage

## Scope

High-signal test-hardening slice that adds regression coverage for the
provider recovery and fallback dispatch contract. Tests-first; production
edits only when a failing test proves a real gap.

Focus areas:

1. `classify_provider_failure()` coverage for all four agent adapters
   (Codex, Claude Code, Gemini, OpenCode) across capacity, auth, quota,
   usage-limit, and timeout stderr shapes.
2. Fallback attempt inheritance proving every required field flows from
   source to fallback workspace.
3. Non-transient deterministic failure termination (repeated conformance
   gaps, no-progress fingerprints) hitting a finite terminal state with
   actionable details.
4. No-work cleanup preserving logs/artifacts/recovery metadata before
   container teardown.
5. Resolve the sole stale TODO about fallback dispatch in
   `test_executor_error_paths.py`.

## Intended Files and Modules

Tests first — no production edits in any of these unless a failing test
drives a small, targeted fix:

- `tests/unit/service/test_provider_recovery.py`
  - Add `classify_provider_failure()` round-trip tests for Codex, Claude,
    Gemini, and OpenCode stderr shapes → retryable provider reason codes.
  - Add a fallback inheritance test that covers *every* field in the
    contract explicitly: `test_commands`, `requested_tier` (via
    task_policy), `profile_ref`, `requested_profile`, `resolved_profile`,
    `owned_paths`, `auto_merge`, `initial_review_grace_period_seconds`,
    `task_prompt`, plan/conformance artifacts (monitor policy inside
    task_policy), and canonical attempt lineage (`is_canonical_for_merge`,
    `parent_attempt_id`, `redispatch_from_attempt_id`).
  - Add terminal-state tests: repeated non-transient deterministic
    provider failures → terminal with `REPEATED_PROVIDER_FAILURE_FINGERPRINT`;
    exhausted fallback targets → `PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED`.
  - Add a "no-work" provider failure test proving a no-work failure is
    classified/retryable (not silently skipped).

- `tests/unit/service/test_workspace_retry.py`
  - Add a conformance retry loop termination test: repeated `PLAN_CONFORMANCE_UNSATISFIED`
    with the same gaps → eventually terminal (finite retry cap reached).
  - Add a test that terminal conformance retries carry actionable details
    (reason_code, retry_attempt_number, remaining gaps in payload).

- `tests/unit/control/test_executor_error_paths.py`
  - Replace the TODO comment in `test_agent_run_capacity_exhausted_surfaces_structured_failure`
    with executable assertions that verify provider recovery metadata
    flows through the executor's failure path (reason_code, provider, model,
    retryable flag, recommended_action in the workspace failure_details/
    events).  Keep the existing test as a concrete regression proof
    that the structured failure path works end-to-end; remove the TODO.

- `tests/unit/service/test_gc.py`
  - Add a test proving that a no-work superseded workspace is NOT a GC
    candidate until logs + artifacts + recovery metadata are persisted
    (i.e. the workspace row has failure_details, events, and task_attempt
    lineage).  If the existing tests already prove this implicitly, add
    assertions making it explicit.

Potentially touched production files (only if a failing test demands it):

- `src/awf/adapters/provider_failures.py` — only if a new stderr shape
  is uncovered but not classified.
- `src/awf/service/provider_recovery.py` — only if inheritance or terminal
  logic has a proven gap.
- `src/awf/control/executor.py` — only if the executor fails to propagate
  provider recovery metadata through the failure path.

## TDD Sequence

1. **Provider failure classification breadth** (test_provider_recovery.py)
   - Write failing tests for each adapter (Codex, Claude, Gemini, OpenCode)
     with realistic stderr shapes for capacity, auth, quota, usage-limit,
     and timeout errors.
   - Assert `classify_provider_failure()` returns a non-None
     `ProviderFailureClassification` with correct `failure_type`, `retryable=True`,
     `reason_code`, and `provider` inference.
   - If any adapter stderr shape is missed, add the marker to
     `provider_failures.py` — smallest possible edit.

2. **Fallback inheritance completeness** (test_provider_recovery.py)
   - Write a failing test that creates a source workspace with *all* v2
     fields populated (test_commands, requested_tier in task_policy,
     profile_ref, profile dicts, owned_paths, auto_merge, review_grace,
     task_prompt with plan/conformance artifact references), fails it,
     calls `create_provider_recovery_attempt_row()`, and asserts **every**
     field is present in the fallback workspace.
   - Also assert that `task_policy.provider_recovery_state.source_workspace_id`
     and `task_policy.provider_recovery_state.source_canonical_attempt_id`
     are set.

3. **Terminal state — repeated fingerprints** (test_provider_recovery.py)
   - Write a failing test: same non-transient failure fingerprint
     presented three times → terminal decision with
     `REPEATED_PROVIDER_FAILURE_FINGERPRINT`.
   - Write a failing test: all fallback targets exhausted (state shows
     `fallback_attempt_number >= max_fallback_attempts`) → terminal
     `PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED` with actionable details.

4. **Conformance retry loop termination** (test_workspace_retry.py)
   - Write a failing test: workspace fails with `PLAN_CONFORMANCE_UNSATISFIED`,
     is retried three times with the same gaps, eventually reaches a
     terminal state (or max retries from task_policy).
   - Assert the retry payload includes `retry_attempt_number` and the
     remaining gaps so an operator has actionable context.

5. **No-work cleanup with metadata retention** (test_gc.py)
   - Write a failing test: a no-work superseded workspace is a GC candidate
     *only after* its failure_details, recovery events, and task_attempt
     lineage are fully persisted.
   - Assert that the candidate payload includes the recovery metadata
     reference.

6. **TODO resolution** (test_executor_error_paths.py)
   - Update `test_agent_run_capacity_exhausted_surfaces_structured_failure`
     to additionally assert that the workspace's `failure_details` dict
     and the terminal event's payload include provider recovery metadata
     (`provider`, `model`, `retryable`, `recommended_action`).
   - Remove the TODO comment.
   - The test should now serve as a positive regression test proving the
     executor correctly surfaces structured provider failures.  There is
     no separate "fallback dispatch in executor" feature to implement here;
     the executor's job is to surface the failure so that
     `create_provider_recovery_attempt_row()` can consume it later.

7. **Fix any production gaps** revealed by failing tests.
   - Keep edits minimal. Only fix what a failing test proves is broken.

8. **Validation**
   - Run targeted tests first, then broader suite, then lint/typecheck.

## Validation Commands

Targeted:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_recovery.py tests/unit/service/test_workspace_retry.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths.py -q
```

Broader:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit -q
```

## Risks and Assumptions

- SQLite in-memory tests suffice for the classification, decision, and
  lineage logic. No integration test with live containers is required
  for this slice.
- The executor's current behavior (surfacing structured provider failures
  to the workspace row and events) is already correct; the TODO was a
  forward-looking note that is now resolved by the `provider_recovery.py`
  module handling fallback dispatch at the service layer.
- The existing `fallback_attempt_inherits_lineage_and_workspace_policy`
  test covers many fields but not all; the new inheritance test
  intentionally duplicates some assertions to be comprehensive.
- No schema changes, migrations, or new tables are expected.

## Non-Goals

- Do not implement executor-side automatic fallback dispatch (e.g.
  calling `create_provider_recovery_attempt_row()` from inside
  `WorkspaceExecutor.execute()`).  That remains a separate architectural
  decision for a future slice.
- Do not add new API endpoints, CLI flags, or configuration formats.
- Do not modify the workspace profile, Docker lifecycle, or compose logic.
- Do not change `retry_workspace_row()` retry semantics beyond what
  failing tests prove is broken.
- Do not switch branches, push, or rebase.
