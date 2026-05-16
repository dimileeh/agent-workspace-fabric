# Plan: Plan Conformance Recovery Slice

## Goal

Implement a narrow systemic recovery slice for Plan -> Execute -> Compare failures.
When plan conformance is exhausted, AWF should classify it with a specific
reason code, persist the final conformance evidence, expose that evidence and
salvage information to operators, and steer retry/redispatch attempts toward
the remaining gaps instead of restarting blindly. Also raise the planning
iteration default to 3 with documented settings/profile precedence.

## Current Anchors

- Plan/execute/compare helpers live in `src/awf/runtime/planning.py`.
- The executor loop and terminal failure transition live in
  `src/awf/control/executor.py`, especially
  `_run_agent_task_with_optional_planning()` and `_mark_failed()`.
- Workspace events already support structured JSON payloads through
  `WorkspaceRepository.add_event()` and `transition(..., payload=...)`.
- Failed-workspace retry/redispatch cloning lives in
  `src/awf/service/workspaces.py::retry_workspace_row()`.
- Workspace API projection lives in `src/awf/api/schemas.py` and
  `src/awf/service/workspaces.py::workspace_response()`.
- Failure analysis grouping and examples live in `src/awf/service/metrics.py`
  and `src/awf/api/routes/metrics.py`.
- Runtime settings live in `src/awf/common/config.py` and service settings in
  `src/awf/service/config.py`.
- Profile planning schema lives in `src/awf/profiles/models.py`.
- AWF self-dogfood planning config is `.awf/workspace.yml`.

## Intended Files And Modules To Touch

- `tests/unit/runtime/test_planning.py`
  - Add tests for conformance report serialization/evidence helpers and retry
    prompt construction if helper functions are added in `runtime.planning`.
  - Cover `reason_code` parsing/defaulting and sanitized structured gaps.

- `tests/unit/control/test_executor_coverage_edges.py`
  - Add focused helper-level tests proving exhausted conformance returns a
    structured planning failure object rather than only a string.
  - Assert final `summary`, `gaps`, `reason_code`, `iterations_used`,
    `plan_path`, and `report_path` are preserved.

- `tests/unit/control/test_executor.py`
  - Add an end-to-end executor test for exhausted conformance:
    `Workspace.failure_reason` remains backward-compatible as
    `agent_failure`, the terminal event reason is
    `PLAN_CONFORMANCE_UNSATISFIED`, and event payload/details include the final
    conformance evidence.
  - Add salvage-hint assertions for preserved worktree path and local/remote
    branch reference when available.

- `tests/unit/service/test_workspace_retry.py`
  - Add tests that retrying a workspace failed with
    `PLAN_CONFORMANCE_UNSATISFIED` enriches the new workspace prompt with the
    final conformance gaps and asks the agent to finish them.
  - Assert ordinary validation/infrastructure retries keep the current cloned
    prompt behavior.
  - Assert retry-created events/operation payloads reference the conformance
    evidence source without duplicating large or unsafe content.

- `tests/unit/api/test_workspace_retry.py`
  - Update the existing prompt clone assertion only for the conformance-failure
    case; default retry should still preserve exact prompt text.

- `tests/unit/service/test_workspace_response.py`
  - Add response projection tests for structured `failure_details` or a
    similarly named compact field exposing conformance evidence and salvage
    hints.

- `tests/unit/api/test_failure_analysis.py` and
  `tests/unit/service/test_metrics.py`
  - Add failure-analysis tests showing examples and root-cause clusters expose
    the specific reason code/details for plan conformance unsatisfied, even
    though the coarse `failure_reason` remains `agent_failure`.

- `tests/unit/profiles/test_profiles.py`
  - Add tests for planning default `max_iterations == 3`.
  - Add precedence tests: explicit profile `planning.max_iterations` wins over
    settings/env default.

- `tests/unit/service/test_config.py` and `tests/unit/service/test_worker.py`
  - Add settings/service-settings tests for
    `AWF_PLANNING_MAX_ITERATIONS_DEFAULT`.
  - Assert worker wiring passes the setting into `ExecutorConfig`.

- `src/awf/runtime/planning.py`
  - Add constants such as `PLAN_CONFORMANCE_UNSATISFIED`.
  - Add a small structured result/evidence type or helper for final conformance
    evidence.
  - Add a retry prompt helper that appends final conformance gaps to the
    original task prompt only for this failure type.

- `src/awf/control/executor.py`
  - Change `_run_agent_task_with_optional_planning()` to return a structured
    failure object for planning/conformance failures while preserving simple
    messages for existing callers/tests.
  - On exhausted conformance, call `_mark_failed()` with
    `failure_reason=FailureReason.agent_failure`,
    `reason_code=PLAN_CONFORMANCE_UNSATISFIED`, and structured event/transition
    payload.
  - Include salvage hints in the failure payload when `worktree_path`,
    `branch_name`, or `remote_push_branch` are known and preserved.
  - Apply the settings-driven default iteration count only when the resolved
    profile did not explicitly set `planning.max_iterations`.

- `src/awf/db/enums.py`
  - Prefer not to add a new coarse `FailureReason`; keep backward-compatible
    `agent_failure`.

- `src/awf/api/schemas.py`
  - Add compact optional response models for failure details and salvage info,
    if exposing this through `WorkspaceResponse`/overview is cleaner than
    requiring clients to inspect raw events.

- `src/awf/service/workspaces.py`
  - Extract latest conformance evidence from source workspace events.
  - Enrich retry workspace prompt only when the terminal specific reason code is
    `PLAN_CONFORMANCE_UNSATISFIED`.
  - Include evidence references in retry events/operation payloads.
  - Add response projection for failure details/salvage info if adopted.

- `src/awf/service/metrics.py` and `src/awf/api/routes/metrics.py`
  - Extend failed examples/root-cause cluster data with `reason_code` and
    compact `details`/`salvage` fields.
  - Add a deterministic recommended action for unsatisfied conformance.

- `src/awf/common/config.py`, `src/awf/service/config.py`,
  `src/awf/service/worker.py`, `src/awf/control/executor.py`
  - Add and wire `planning_max_iterations_default: int = 3`.
  - Use env name `AWF_PLANNING_MAX_ITERATIONS_DEFAULT`.
  - Chosen precedence: explicit profile value wins; env/settings default only
    applies when `planning.max_iterations` was omitted from the profile.

- `.env.example`
  - Add `AWF_PLANNING_MAX_ITERATIONS_DEFAULT=3`.

- `.awf/workspace.yml`
  - Change AWF self-dogfood `planning.max_iterations` from 2 to 3.

No migrations should be needed because structured evidence can be stored in
existing `workspace_events.payload` and response/failure-analysis projection can
derive from those events.

## Tests To Write First

1. Runtime planning helper tests:
   - Parse/report helpers preserve a specific `reason_code`.
   - Evidence serialization returns `summary`, `gaps`, `reason_code`, paths, and
     iteration counts without unsafe free-form expansion.
   - Retry prompt helper includes the original task, final gaps, and an explicit
     instruction to finish remaining conformance work.

2. Executor helper tests:
   - Exhausted conformance after `max_iterations=0` returns/records structured
     evidence with `PLAN_CONFORMANCE_UNSATISFIED`.
   - Existing invalid plan path, missing plan file, and conformance side-effect
     failures keep their current messages and classification.

3. Executor end-to-end failure test:
   - Seed a planning-required workspace, queue conformance `needs_iteration`,
     and execute.
   - Expected red before implementation: terminal event reason is generic
     `AGENT_FAILURE`, no structured payload exists, and no salvage hint is
     exposed.
   - Expected green: `failure_reason == "agent_failure"`,
     terminal event reason is `PLAN_CONFORMANCE_UNSATISFIED`, and payload
     contains conformance evidence plus worktree/branch salvage hints.

4. Retry service/API tests:
   - A failed source workspace with a terminal conformance-unsatisfied event
     produces a new requested workspace whose prompt asks the agent to finish
     the remaining gaps.
   - A normal validation-failed retry still clones the original prompt exactly.
   - Retry events and operation result include the source reason code/evidence
     reference.

5. Workspace response and failure-analysis tests:
   - `workspace_response()` exposes compact conformance failure details and
     salvage hint/path/branch.
   - `/v1/metrics/failures/summary` examples and clusters expose
     `PLAN_CONFORMANCE_UNSATISFIED` details even while grouping remains
     compatible with coarse `agent_failure`.

6. Config/profile precedence tests:
   - `Settings(_env_file=None).planning_max_iterations_default == 3`.
   - `resolve_service_settings()` exposes the same value and honors
     `AWF_PLANNING_MAX_ITERATIONS_DEFAULT`.
   - Worker wiring passes the configured default to `ExecutorConfig`.
   - A profile with omitted `planning.max_iterations` uses the configured
     default.
   - A profile that explicitly sets `max_iterations: 1` keeps 1 even when the
     env default is 3 or another valid value.
   - `.awf/workspace.yml` self-dogfood profile declares 3.

## Implementation Sequence

1. Add the failing tests above, starting with runtime/helper tests and the
   executor exhausted-conformance test.
2. Add `PLAN_CONFORMANCE_UNSATISFIED` and a small evidence/result type in
   `runtime.planning`.
3. Refactor executor planning handling just enough to return structured failure
   details for exhausted conformance.
4. Extend `_mark_failed()` or add a narrow companion path so terminal
   `workspace.state_changed` receives a structured payload and optional salvage
   hints.
5. Add conformance evidence extraction helpers in `service.workspaces` for
   workspace response and retry prompt construction.
6. Enrich retry prompts only for the conformance-unsatisfied reason code; keep
   all other retry prompts unchanged.
7. Extend failure-analysis dataclasses/API response models with optional
   reason-code/details/salvage fields and keep existing fields backward
   compatible.
8. Add `planning_max_iterations_default` settings/service wiring and executor
   profile-precedence handling.
9. Update `.env.example` and `.awf/workspace.yml`.
10. Run focused tests, then ruff and mypy.

## Validation Commands

Focused TDD loops:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry.py tests/unit/api/test_workspace_retry.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_response.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py tests/unit/api/test_failure_analysis.py -q
uv run --python 3.12 --extra dev pytest tests/unit/profiles/test_profiles.py tests/unit/service/test_config.py tests/unit/service/test_worker.py -q
```

Requested quality gates:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

If the executor/failure-analysis changes touch more shared behavior than
expected, also run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit -q
```

## Risks

- Keeping `failure_reason` backward-compatible as `agent_failure` while adding a
  specific reason code means operators must look at event/details fields for the
  precise cause. Tests should lock this down so it is intentional, not hidden.
- Response-model changes can break API fixtures if new fields are required.
  Make new detail fields optional/defaulted.
- Retry prompt enrichment must not append unbounded report text or leak secrets;
  use only sanitized final summary/gaps from conformance evidence.
- Event-derived evidence can be missing for legacy rows; response and retry
  code must handle that gracefully and fall back to the old prompt/message.
- Detecting whether profile `max_iterations` was explicitly set relies on
  Pydantic `model_fields_set`. Tests should cover inline profiles, resolved
  repo profiles, and stored profile snapshots enough to avoid accidental env
  override of explicit profile values.

## Assumptions

- Existing `workspace_events.payload` is sufficient durable storage for
  conformance evidence; no schema migration is needed.
- Failed workspaces normally keep their worktree until cleanup, so salvage
  hints can point to the managed worktree path and branch references without
  guaranteeing the path still exists forever.
- `PLAN_CONFORMANCE_UNSATISFIED` is a specific reason code, not a new coarse
  `FailureReason`.
- The env/settings default is a fallback for omitted profile values, not a
  global override of explicit profile policy.

## Non-Goals

- Do not redesign the planner or Plan -> Execute -> Compare loop.
- Do not add arbitrary shell/Docker access or new operator repair commands.
- Do not add database migrations unless existing event payload storage proves
  insufficient.
- Do not change PR monitor recovery semantics.
- Do not lower coverage targets, validation tiers, linting, or type-checking
  quality gates.
