# Plan: tracked docker compose exec cleanup

## Goal

Fix AWF runtime safety so a timed-out or cancelled `docker compose exec` invocation cannot leave the original in-container agent or validation process tree alive while the executor continues into commit, validation, fix passes, push, or PR monitoring.

The change will be scoped to the shared command execution surface for in-container commands. Non-docker subprocess behavior should stay unchanged.

## Intended Files And Modules

- `src/awf/common/commands.py`
  - Keep the existing generic subprocess runner behavior for non-docker commands.
  - Add, or expose support used by the compose-exec helper for timeout/cancellation cleanup results if needed.

- `src/awf/common/compose_exec.py` (new)
  - Centralize AWF-tracked `docker compose exec` argument construction and cleanup.
  - Generate a unique AWF invocation id per in-container command.
  - Wrap the in-container command with an AWF-owned shell wrapper that:
    - exports `AWF_EXEC_INVOCATION_ID`;
    - starts the real command in a distinct process group when `setsid` is available;
    - writes lightweight invocation metadata under `/tmp/awf-exec/<id>/`;
    - preserves the original argv without shell-string joining.
  - Provide a targeted cleanup command that re-enters only the same compose project/service and terminates processes for that invocation by process group and exact `AWF_EXEC_INVOCATION_ID` matches in `/proc`.
  - Raise a typed error such as `ComposeExecCleanupError` when cleanup cannot prove the tagged process tree is gone.

- `src/awf/adapters/base.py`
  - Replace direct adapter construction of `docker compose exec ... agent <cli args>` with the tracked compose-exec helper.
  - On agent timeout or cancellation, invoke targeted cleanup before raising the agent error.
  - Let cleanup failures surface distinctly instead of becoming salvageable `AgentRunError`s.
  - Preserve stdout/stderr streaming and existing timeout reason mapping for successful cleanup cases.

- `src/awf/runtime/validation.py`
  - Replace direct validation `docker compose exec` construction with the same tracked helper.
  - Stop using bare `asyncio.wait_for(...)` around compose-exec validation commands in a way that only kills the local client.
  - On phase timeout, return `PHASE_TIMEOUT` only after cleanup succeeds or the process tree is proven absent.
  - Propagate `ComposeExecCleanupError` on cleanup failure so the executor can fail infrastructure rather than continue fix passes.

- `src/awf/control/executor.py`
  - Catch `ComposeExecCleanupError` around:
    - initial agent runs;
    - validation execution;
    - validation fix-pass agent runs.
  - Mark the workspace failed with `FailureReason.infrastructure_failure` and reason code `EXEC_PROCESS_CLEANUP_FAILED`.
  - Ensure no post-agent commit, validation, validation fix pass, push, or monitor handoff starts after a cleanup failure.
  - Keep stale-status rechecks before fix passes, and add a focused regression for cancellation/destroy winning before fix-pass execution.
  - Finish any active validation run/operation with `EXEC_PROCESS_CLEANUP_FAILED` when the cleanup failure occurs during validation.

- `src/awf/runtime/pr_monitor_runner.py`
  - Handle `ComposeExecCleanupError` from monitor-driven adapter runs and terminate the monitor/workspace with infrastructure failure instead of continuing comment, CI-fix, sync-base, push, or merge work.
  - Keep this small and limited to adapter-run call sites.

- `tests/unit/common/test_compose_exec_cleanup.py` (new)
  - Unit tests for wrapper/cleanup command construction and targeted cleanup semantics with fake runners.

- `tests/unit/adapters/test_adapters.py`
  - Update adapter argv expectations to account for the AWF wrapper while still asserting CLI-specific args and prompts are preserved.
  - Add agent timeout cleanup coverage.
  - Add normal successful adapter run coverage that cleanup is not invoked.

- `tests/unit/runtime/test_validation.py`
  - Update exact argv assertions for validation commands to include the AWF wrapper.
  - Add validation timeout cleanup coverage.
  - Add cleanup failure propagation coverage.
  - Add normal successful validation coverage that cleanup is not invoked.

- `tests/unit/control/test_executor_validation_fix_cycle.py`
  - Add executor-level tests for cleanup failure blocking validation/fix continuation.
  - Add cancellation/destroy status winning before a validation fix pass starts.

- `tests/unit/control/test_executor.py` or `tests/unit/control/test_executor_error_paths.py`
  - Add initial agent timeout cleanup failure mapping to `infrastructure_failure` with `EXEC_PROCESS_CLEANUP_FAILED`, if it fits better than the fix-cycle file.

- `tests/unit/runtime/test_pr_monitor_runner.py` or coverage-edge companion
  - Add a small monitor regression that cleanup failure from an adapter run terminates instead of continuing to push/merge.

## Tests To Write First

1. `tests/unit/common/test_compose_exec_cleanup.py`
   - `test_builds_tracked_exec_wrapper_with_unique_invocation_id`
     - Asserts the command remains `docker compose exec -T -w /workspace agent ...`.
     - Asserts original CLI argv is preserved after the wrapper.
     - Asserts `AWF_EXEC_INVOCATION_ID` is introduced by the wrapper, not by broad process-name matching.
   - `test_cleanup_command_targets_only_invocation_id`
     - Asserts cleanup command is another `docker compose exec` into the same project/file/service.
     - Asserts cleanup uses exact invocation id and does not include `pkill claude`, `pkill codex`, or other broad process-name kills.

2. `tests/unit/adapters/test_adapters.py`
   - `test_timeout_invokes_targeted_in_container_cleanup`
     - Use a fake streaming runner returning `CommandResult(returncode=124, reason_code="COMMAND_IDLE_TIMEOUT")`.
     - Assert adapter raises `AgentRunError` with `AGENT_IDLE_TIMEOUT` only after one targeted cleanup call.
     - Assert cleanup call references the same invocation id as the wrapped agent command.
   - `test_cleanup_failure_surfaces_distinct_error`
     - Timeout result followed by cleanup nonzero.
     - Assert `ComposeExecCleanupError` and reason code `EXEC_PROCESS_CLEANUP_FAILED`.
   - `test_successful_agent_run_does_not_invoke_cleanup`
     - Green result records only the agent exec call.

3. `tests/unit/runtime/test_validation.py`
   - `test_exec_timeout_invokes_targeted_cleanup_before_phase_timeout`
     - Profile command timeout returns `PHASE_TIMEOUT` only after cleanup success.
   - `test_exec_cleanup_failure_raises_infrastructure_cleanup_error`
     - Timeout followed by cleanup nonzero raises `ComposeExecCleanupError`.
   - `test_exec_success_does_not_cleanup`
     - Green validation command records no cleanup call.

4. `tests/unit/control/test_executor_validation_fix_cycle.py`
   - `test_agent_cleanup_failure_fails_infrastructure_before_validation`
     - Initial agent command times out; cleanup fails.
     - Assert workspace status is `failed`, `failure_reason == infrastructure_failure`, latest reason code is `EXEC_PROCESS_CLEANUP_FAILED`, and no validation/push/fix-pass commands were consumed.
   - `test_validation_cleanup_failure_does_not_start_fix_pass`
     - Initial agent succeeds and commits.
     - Validation command times out; cleanup fails.
     - Assert validation run/operation failed with `EXEC_PROCESS_CLEANUP_FAILED`, workspace failed infrastructure, and no second adapter/fix-pass command appears.
   - `test_cancelled_or_destroying_status_wins_before_fix_pass`
     - Simulate status moving to `cancelled` or `destroying` after a failed validation result and before `validation_fix_agent_run`.
     - Assert no fix-pass adapter command starts and workspace remains in the operator-requested status.

5. `tests/unit/common/test_command_watchdogs.py`
   - Keep existing non-docker subprocess watchdog tests passing.
   - Add a narrow non-docker regression only if the helper changes the generic runner surface.

6. `tests/unit/runtime/test_pr_monitor_runner.py`
   - `test_monitor_adapter_cleanup_failure_terminates_without_push`
     - Adapter raises `ComposeExecCleanupError`.
     - Assert monitor marks infrastructure failure or terminates through its existing failure path and does not push/merge.

## Implementation Outline

1. Add the compose-exec helper and wrapper.
   - Build tracked commands as:
     - `docker compose --project-name <project> --file <compose> exec -T -w /workspace agent sh -lc <wrapper> awf-exec <invocation_id> <original argv...>`
   - The wrapper must use `exec "$@"` for the real command so the original CLI receives unchanged argv.
   - Use `setsid` when present so the real command becomes a process-group leader.
   - Export `AWF_EXEC_INVOCATION_ID=<id>` so descendants can be found even if they outlive the original process group.

2. Add targeted cleanup.
   - Cleanup runs through `docker compose exec` against the same compose project/file/service.
   - It reads the wrapper metadata pid/pgid if present, sends `TERM`, waits briefly, then sends `KILL` if needed.
   - It also scans `/proc/*/environ` for exact `AWF_EXEC_INVOCATION_ID=<id>` matches and kills those pids.
   - It exits zero only when no tagged pids remain, or when no tagged process was present.
   - It exits nonzero with a useful stderr message when tagged processes remain or cleanup itself cannot inspect/kill them.

3. Route agents through the helper.
   - Preserve current streaming, stdin closure, log-store sinks, and timeout settings.
   - On timeout result codes (`COMMAND_TIMEOUT`, `COMMAND_IDLE_TIMEOUT`) call cleanup before mapping to `AGENT_TIMEOUT` or `AGENT_IDLE_TIMEOUT`.
   - On `asyncio.CancelledError`, shield a best-effort cleanup attempt and re-raise cancellation after cleanup handling.
   - On cleanup failure, raise `ComposeExecCleanupError`.

4. Route validation through the helper.
   - Use the helper for phase commands, healthchecks, and coverage commands.
   - Prefer streaming runner timeouts (`wall_timeout_seconds`) so timeout handling and cleanup stay in one path.
   - Preserve artifact writing and log sink behavior.
   - Return `PHASE_TIMEOUT` only when cleanup succeeded.

5. Fail safely at executor boundaries.
   - Initial agent cleanup failure: mark from `running` to `failed` with `infrastructure_failure` and `EXEC_PROCESS_CLEANUP_FAILED`; return before commit/validation.
   - Validation cleanup failure: finish validation run/operations with `EXEC_PROCESS_CLEANUP_FAILED`, mark from `validating` to `failed`, return before fix pass.
   - Fix-pass cleanup failure: mark from `validating` to `failed`, return before commit/revalidation.
   - Keep existing stale-status checks so operator cancellation/destroy prevents fix passes.

6. Add structured logs/events.
   - Log `compose_exec.cleanup.start`, `.succeeded`, `.absent`, and `.failed` with `workspace_id`, `compose_project`, `service`, `source`, `label`, `invocation_id`, and `reason_code`.
   - Add a workspace event on cleanup failure with event type such as `workspace.exec_process_cleanup_failed`, reason code `EXEC_PROCESS_CLEANUP_FAILED`, and a bounded stderr/message payload.
   - Do not log prompts, tokens, full command strings, or secrets.

7. Update existing tests for wrapper-aware argv.
   - Keep assertions focused on stable invariants:
     - compose prefix;
     - service/workdir;
     - wrapper marker/invocation id;
     - original CLI argv and prompt are still present in order.
   - Avoid brittle full-list assertions where the wrapper script text is not the behavior under test.

## Validation Commands

Run the narrow suites first while developing:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_compose_exec_cleanup.py tests/unit/adapters/test_adapters.py tests/unit/runtime/test_validation.py tests/unit/control/test_executor_validation_fix_cycle.py -q
```

Then run the required task validation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common tests/unit/adapters tests/unit/control tests/unit/runtime -q
uv run --python 3.12 --extra dev pytest --cov=awf --cov-report=term-missing
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

## Risks

- Some minimal container images may not include `setsid`. The wrapper must still work by relying on `AWF_EXEC_INVOCATION_ID` `/proc` matching.
- Some descendants may deliberately clear their environment or start a new session. Combining process-group kill with environment scanning reduces this, but cannot guarantee cleanup of a hostile process that intentionally detaches and scrubs metadata.
- Cleanup itself uses `docker compose exec`; if the container is stopped or Docker is unavailable, absence can be proven only when the stack/container is gone. Ambiguous cleanup failures must fail infrastructure rather than continue.
- Existing tests assert exact docker argv in a few places. These need behavior-focused updates so the wrapper can be introduced without weakening coverage.
- Cancellation cleanup is timing-sensitive. Tests should use deterministic fake runners for unit coverage and leave real-process stress to integration follow-up if needed.

## Assumptions

- AWF-managed workspace containers are Linux containers with `/proc` available.
- The agent service is still named `agent` for these execution paths.
- `docker compose exec -T` can run a cleanup shell in the same service after the original exec client timed out, unless the container/stack has already stopped.
- If the target stack/container no longer exists, the original process tree is absent and cleanup can be treated as successful absence when the helper can distinguish that condition.
- A cleanup failure during timeout/cancellation is an infrastructure failure, not an agent or validation failure.

## Explicit Non-goals

- Do not kill by broad process names such as `pkill claude`, `pkill codex`, `pkill pytest`, or `killall`.
- Do not change AWF branch management, push behavior, PR creation, or merge policy.
- Do not add retries that hide timeout or cleanup reason codes.
- Do not reduce coverage thresholds, skip tests, xfail tests, or weaken quality gates.
- Do not change profile schema unless implementation proves an existing field is insufficient.
- Do not try to solve intentionally malicious in-container daemonization beyond targeted process group and invocation-id cleanup.
