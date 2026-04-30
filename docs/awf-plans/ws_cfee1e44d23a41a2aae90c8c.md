# Plan: P1 provider-resilience cleanup for no-work failed terminal workspaces

## Context

Some terminal workspaces can fail with no active work, leaving agent containers stuck in an idle command state (e.g., `sleep infinity`). The control-plane currently treats many terminal failures as preserved by default, so these resources may remain up in Docker even when workspace state is `failed`/`superseded` and no process is doing agent work. This plan defines a narrow, test-first cleanup path for that case while preserving required logs/artifacts and avoiding disruption of active states.

## Scope

- Only implement a cleanup-safety slice for terminal no-work failure states.
- Preserve existing behavior for active/validating/pushing/monitoring-pr workspaces.
- Keep all changes limited to behavior needed for:
  - terminal failure/superseded detection, and
  - cleanup of idle no-work containers/networks/pressure directories.

## Intended files/modules to touch

- `tests/unit/service/test_orphans.py`
  - add/extend tests around orphan/resource retention classification for terminal no-work failure states.
- `tests/unit/service/test_gc.py`
  - add/extend tests proving idempotent cleanup behavior for terminal no-work failed/superseded workspaces.
- `tests/unit/service/test_status.py`
  - add/extend tests asserting cleanup readiness/surface visibility and orphan/process flags are observable.
- `tests/unit/service/test_controls.py`
  - add/extend tests asserting cleanup control commands remain no-op for non-terminal/active states and are effective for eligible terminal no-work states.
- `tests/unit/node/test_cleanup.py`
  - add/extend tests proving node cleanup removes containers/networks/pressure and notifies preserved artifacts/worktree retention policy.
- `tests/unit/api/test_health.py`
  - add/extend assertions for health/doctor status fields surfacing stranded/no-work terminal state.
- `src/awf/service/orphans.py`
  - implement classification rule and reason for terminal failed/superseded no-work state cleanup readiness.
- `src/awf/service/gc.py`
  - keep container/network/pressure removals idempotent and aligned with policy exceptions for preserved data.
- `src/awf/node/cleanup.py`
  - add/adjust cleanup behavior to explicitly handle terminal no-work no-process case using Docker inspection metadata without restarting Docker.
- `src/awf/service/controls.py`
  - route cleanup control path to use updated readiness state and expose explicit skip behavior for active/validating/pushing/monitoring-pr workspaces.
- `src/awf/service/status.py`
  - ensure cleanup readiness and stranded resource signals remain consistent and observable via status.
- `src/awf/runtime/inspection.py`
  - wire in process-command/entrypoint inspection required to detect `sleep infinity`-style idle container in terminal workspaces (if not already sufficient via current runtime fields).
- `src/awf/service/workspace_runtime_health.py`
  - if needed, extend stranded detection reason mapping to expose the no-work terminal condition in health/doctor output.

## Strict TDD plan (tests first)

1. Add failing unit tests for terminal no-work failed/superseded resources are marked cleanup-ready
   - file: `tests/unit/service/test_orphans.py`
   - scenario: workspace status terminal + failure mode with no active agent process should be in terminal-retention-eligible cleanup reason.
   - scenario: terminal failed states with expected active work should remain preserved.

2. Add failing unit tests for idempotent cleanup execution
   - file: `tests/unit/service/test_gc.py`
   - scenario: running cleanup twice is safe and does not error.
   - scenario: cleanup removes only terminal no-work candidates while preserving non-terminal and explicitly preserved states.

3. Add failing tests for status/health observability
   - file: `tests/unit/service/test_status.py`
     - cleanup readiness includes no-work terminal candidate and reason.
   - file: `tests/unit/api/test_health.py`
     - no-work terminal condition appears in health/doctor summaries and is queryable through existing status surfaces.

4. Add failing control-path tests
   - file: `tests/unit/service/test_controls.py`
     - cleanup control for active/validating/pushing/monitoring-pr returns skipped/no-op.
     - cleanup control for terminal no-work failed/superseded workspace triggers cleanup and persists artifact/log retention.

5. Add failing node cleanup tests
   - file: `tests/unit/node/test_cleanup.py`
     - removes orphaned agent container and associated network(s).
     - removes pressure directory paths.
     - preserves required metadata/log/artifact directories unless policy explicitly opts in.

6. Implement smallest production code changes in listed modules.

7. Re-run tests incrementally, then run requested command list.

## Test execution order

1. `uv run --python 3.12 --extra dev ruff check src/awf/service src/awf/node tests/unit/service tests/unit/node tests/unit/api`
2. `uv run --python 3.12 --extra dev mypy src/awf`
3. `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc.py tests/unit/service/test_orphans.py tests/unit/service/test_status.py tests/unit/service/test_controls.py tests/unit/api/test_health.py tests/unit/node/test_cleanup.py -q`

## Implementation notes

- Maintain current policy default that terminal `failed` workspaces are preserved unless they meet explicit no-work failed/superseded criteria.
- Keep cleanup logic idempotent; repeated cleanup calls should remain safe.
- Preserve logs/artifacts and workspace worktree by default.
- Container/runtime checks should use existing Docker metadata (no service restart) to infer no-work process.
- Use existing status/event pathways (`status`, `doctor`, `workspace_cleanup` surface) for observability.
- Ensure cleanup state changes are explicit with reason codes and structured events.

## Risks and assumptions

### Risks
- Container metadata is heterogeneous across environments; command/path heuristics for “no active agent process” may false-positive and require a narrow matching strategy.
- Some terminal resources may have mixed container states (e.g., exited after failed init) and need conservative classification to avoid over-cleanup.
- Overlap with active workspace control planes may cause stale state races; cleanup must strictly gate on terminal states.

### Assumptions
- Existing workspace status semantics remain unchanged: `active`, `validating`, `pushing`, `monitoring_pr` are non-terminal and must not be removed.
- Worktree preservation policy remains the default and can be changed only via explicit config/policy path.
- Event/status output contract can be extended only via additive fields/reasons.

### Explicit non-goals
- Do not alter authentication, planning-scope, provider credentials, or capacity logic.
- Do not implement broad orchestration refactors outside service/GC/node cleanup surfaces.
- Do not restart or rebuild Docker daemon locally.
- Do not modify migration, lockfiles, or unrelated docs.
- Do not perform broad monkeypatching/behavior bypasses in tests.

## Exit criteria

- New tests fail before changes and pass after implementation.
- Cleanup path handles terminal no-work failed/superseded workspaces deterministically and leaves active/validating/pushing/monitoring-pr unaffected.
- Logs/artifacts/metadata are preserved by default.
- Cleanup remains idempotent and observable through status/health/doctor output.
- No coverage or policy regressions introduced in touched areas.
