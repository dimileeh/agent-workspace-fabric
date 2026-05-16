# Plan: targeted cleanup for timed-out compose exec commands

## Objective

Make AWF fail safe when an agent, validation command, or monitor-driven coding CLI run launched through `docker compose exec` times out or is cancelled. Killing the local compose client is not enough; AWF must also terminate or prove absence of the matching in-container process tree before it continues to commits, validation, fix passes, push, or PR monitoring.

The fix will stay scoped to runtime command execution, agent adapters, validation, executor cancellation/status checks, and monitor adapter call sites. Non-docker subprocess behavior must remain unchanged.

## Intended Files And Modules To Touch

- `src/awf/common/compose_exec.py`
  - Own the tracked `docker compose exec` helper.
  - Build wrapped exec argv with a unique AWF invocation id.
  - Export `AWF_EXEC_INVOCATION_ID` inside the container and preserve original argv without broad process-name matching.
  - Build and run targeted cleanup for the same compose project, compose file, service, workdir, and invocation id.
  - Raise `ComposeExecCleanupError` with reason code `EXEC_PROCESS_CLEANUP_FAILED` when cleanup cannot prove the process tree is gone.
  - Emit structured cleanup start/success/absent/failure logs with bounded stderr.

- `src/awf/common/commands.py`
  - Preserve generic subprocess and fake-runner contracts.
  - Touch only if the compose-exec helper needs a small protocol/result surface adjustment for timeout/cancellation handling.

- `src/awf/adapters/base.py`
  - Route agent CLI invocations through the tracked compose-exec helper.
  - On `COMMAND_TIMEOUT`, `COMMAND_IDLE_TIMEOUT`, or `asyncio.CancelledError`, run targeted cleanup for that invocation before returning/raising.
  - Preserve existing `AgentRunError` mappings when cleanup succeeds.
  - Let `ComposeExecCleanupError` propagate distinctly when cleanup fails.
  - Ensure successful agent runs do not invoke cleanup.

- `src/awf/runtime/validation.py`
  - Route profile phase, healthcheck, and coverage commands through the same tracked compose-exec helper.
  - For streaming and non-streaming runners, invoke cleanup after timeout/cancellation before returning `PHASE_TIMEOUT`.
  - Propagate `ComposeExecCleanupError` instead of continuing validation retry/fix logic after ambiguous cleanup.
  - Preserve artifact and log-stream writes for normal success/failure paths.

- `src/awf/control/executor.py`
  - Catch `ComposeExecCleanupError` around setup/pre-agent profile phases, initial agent runs, validation runs, and validation fix-pass agent runs.
  - Mark the workspace `failed` with `FailureReason.infrastructure_failure`, reason code `EXEC_PROCESS_CLEANUP_FAILED`, and an actionable bounded message.
  - Finish active validation run/provenance and pending validate operations with `EXEC_PROCESS_CLEANUP_FAILED` when cleanup fails during validation.
  - Recheck workspace status before validation fix passes so operator `cancelled` or `destroying` status wins and no new fix pass starts.

- `src/awf/runtime/pr_monitor_runner.py`
  - Treat `ComposeExecCleanupError` from monitor-driven adapter runs as infrastructure failure.
  - Stop comment-fix, CI-fix, sync-base, push, and merge continuation after cleanup failure.

- `tests/unit/common/test_compose_exec_cleanup.py`
  - New or updated focused tests for wrapper construction, cleanup command construction, error message bounding, and no broad `pkill claude`/`pkill codex` behavior.

- `tests/unit/adapters/test_adapters.py`
  - Update compose argv assertions to account for the wrapper.
  - Add timeout/cancellation cleanup coverage and cleanup-failure propagation coverage.
  - Assert green agent runs do not cleanup.

- `tests/unit/runtime/test_validation.py`
  - Add validation timeout cleanup, cancellation cleanup, cleanup-failure propagation, artifact/log behavior, and green-run no-cleanup coverage.

- `tests/unit/control/test_executor_validation_fix_cycle.py`
  - Add executor-level regressions for agent cleanup failure before validation, validation cleanup failure before fix pass, and cancel/destroy status winning before a fix pass.

- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py` or `tests/unit/runtime/test_pr_monitor_runner.py`
  - Add monitor regressions showing cleanup failure terminates without push/merge continuation.

## Tests To Write First

1. `tests/unit/common/test_compose_exec_cleanup.py`
   - `test_builds_tracked_exec_wrapper_with_unique_invocation_id`
     - Asserts the command starts with `docker compose ... exec -T -w /workspace agent`.
     - Asserts the wrapper introduces `AWF_EXEC_INVOCATION_ID`.
     - Asserts original CLI argv remains present as argv, not shell-joined into the wrapper.
   - `test_cleanup_command_targets_only_invocation_id`
     - Asserts cleanup re-enters the same compose project/file/service/workdir.
     - Asserts cleanup passes the same invocation id.
     - Asserts cleanup script does not contain broad process-name kills such as `pkill claude`, `pkill codex`, or `killall`.
   - `test_cleanup_failure_message_is_bounded`
     - Asserts operator-facing cleanup messages are capped and retain `EXEC_PROCESS_CLEANUP_FAILED`.

2. `tests/unit/adapters/test_adapters.py`
   - `test_timeout_invokes_targeted_in_container_cleanup`
     - Fake runner returns `COMMAND_IDLE_TIMEOUT` for the agent exec and success for cleanup.
     - Assert `AgentRunError.reason_code == "AGENT_IDLE_TIMEOUT"`.
     - Assert the cleanup invocation id matches the wrapped agent invocation id.
   - `test_cleanup_failure_surfaces_distinct_error`
     - Fake runner returns timeout, then cleanup nonzero.
     - Assert `ComposeExecCleanupError.reason_code == "EXEC_PROCESS_CLEANUP_FAILED"`.
   - `test_cancelled_agent_run_cleans_up_in_container_invocation`
     - Streaming fake raises `asyncio.CancelledError`.
     - Assert cleanup is invoked before cancellation propagates.
   - `test_successful_agent_run_does_not_invoke_cleanup`
     - Green result records only the original agent exec.

3. `tests/unit/runtime/test_validation.py`
   - `test_exec_timeout_invokes_targeted_cleanup_before_phase_timeout`
     - Validation command times out and cleanup succeeds.
     - Assert result reason is `PHASE_TIMEOUT` and cleanup used the same invocation id.
   - `test_exec_cleanup_failure_raises_infrastructure_cleanup_error`
     - Timeout followed by cleanup nonzero raises `ComposeExecCleanupError`.
   - `test_exec_cancelled_invokes_targeted_cleanup`
     - Cancellation path cleans up and re-raises cancellation.
   - `test_exec_success_does_not_cleanup`
     - Successful validation command does not call cleanup.
   - Existing artifact/log-stream tests should be updated only as needed for wrapper-aware argv.

4. `tests/unit/control/test_executor_validation_fix_cycle.py`
   - `test_agent_cleanup_failure_fails_infrastructure_before_validation`
     - Initial agent run times out, cleanup fails.
     - Assert workspace is failed with `infrastructure_failure` and `EXEC_PROCESS_CLEANUP_FAILED`.
     - Assert validation/fix/push work does not start.
   - `test_validation_cleanup_failure_does_not_start_fix_pass`
     - Initial agent succeeds, validation command times out, cleanup fails.
     - Assert validation provenance and pending operations record `EXEC_PROCESS_CLEANUP_FAILED`.
     - Assert no fix-pass adapter invocation occurs.
   - `test_cancelled_or_destroying_status_wins_before_fix_pass`
     - Mutate workspace status to `cancelled` or `destroying` after failed validation and before fix-pass start.
     - Assert the operator-requested status remains and no fix-pass command starts.

5. `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
   - `test_monitor_adapter_cleanup_failure_terminates_without_push`
     - Adapter raises `ComposeExecCleanupError`.
     - Assert workspace/monitor failure records `EXEC_PROCESS_CLEANUP_FAILED`.
     - Assert no push, merge, or further monitor action is attempted.

6. Existing non-docker tests
   - Preserve `tests/unit/common` coverage for `AsyncioSubprocessRunner` and `FakeCommandRunner`.
   - Add a narrow non-docker regression only if `src/awf/common/commands.py` changes.

## Implementation Steps

1. Implement the tracked compose-exec helper.
   - Generate invocation ids such as `awf_<uuidhex>` and validate them against a conservative shell-safe pattern.
   - Build argv as `docker compose --project-name <project> --file <file> exec -T -w /workspace agent sh -lc <wrapper> awf-exec <invocation_id> <original argv...>`.
   - In the wrapper, export `AWF_EXEC_INVOCATION_ID`, write metadata under `/tmp/awf-exec/<id>/`, and prefer `setsid "$@"` so the child has a killable process group.
   - Fall back to `exec "$@"` when `setsid` is unavailable.

2. Implement targeted cleanup.
   - Re-enter the same container with `docker compose exec`.
   - Read recorded pid/pgid metadata and scan `/proc/*/environ` for exact `AWF_EXEC_INVOCATION_ID=<id>`.
   - Send `TERM`, wait briefly, then send `KILL` if tagged processes remain.
   - Exit zero only when tagged processes are gone or already absent.
   - Raise `ComposeExecCleanupError` on ambiguous/nonzero cleanup.

3. Wire agent adapters.
   - Replace direct compose exec construction with `build_tracked_compose_exec`.
   - Use streaming runner timeouts when available.
   - On timeout or cancellation, call `cleanup_compose_exec_invocation`.
   - Preserve log stream close behavior in `finally`.

4. Wire validation runner.
   - Use the helper for every in-container profile command, healthcheck, and coverage command.
   - Keep artifact writes and stream sinks intact.
   - Return `PHASE_TIMEOUT` only after cleanup succeeds.
   - Propagate cleanup failure so executor policy decides terminal state.

5. Fail safely in the executor.
   - Convert cleanup failures to `infrastructure_failure` with `EXEC_PROCESS_CLEANUP_FAILED`.
   - Stop immediately after cleanup failure; do not salvage, validate, fix, push, or monitor.
   - Record validation-run and pending-operation failure metadata when the failure occurs during validation.
   - Ensure status rechecks happen immediately before fix-pass adapter runs and fix-pass commits.

6. Wire PR monitor handling.
   - Catch cleanup failure around monitor adapter runs.
   - Mark/return terminal infrastructure failure through the existing monitor failure path.
   - Assert no follow-on push/merge/comment-loop action happens after the cleanup failure.

7. Update logs/events.
   - Emit `compose_exec.cleanup.start`, `compose_exec.cleanup.succeeded`, `compose_exec.cleanup.absent`, and `compose_exec.cleanup.failed`.
   - Include `workspace_id`, `compose_project`, `service`, `source`, `label`, `invocation_id`, and `reason_code`.
   - Do not log prompts, tokens, full shell commands, or secrets.

## Validation Commands

Run focused suites while developing:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_compose_exec_cleanup.py tests/unit/adapters/test_adapters.py tests/unit/runtime/test_validation.py tests/unit/control/test_executor_validation_fix_cycle.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q
```

Run the required task validation before handoff:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common tests/unit/adapters tests/unit/control tests/unit/runtime -q
uv run --python 3.12 --extra dev pytest --cov=awf --cov-report=term-missing
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

## Risks

- Minimal images may not have `setsid`; cleanup must still work through exact invocation-id `/proc` scanning.
- A process can intentionally detach and scrub inherited environment. The supported guarantee is best-effort targeted cleanup for AWF-launched process trees, with infrastructure failure when absence cannot be proven.
- Cleanup itself depends on Docker/Compose access. If the container is stopped, absence may be provable; if Docker is unavailable or cleanup cannot inspect the container, AWF must fail infrastructure instead of continuing.
- Cancellation paths are timing-sensitive. Unit tests should use deterministic fake runners; real-process integration coverage can be a later addition.
- Existing exact argv assertions may become brittle once the wrapper is inserted. Tests should assert stable invariants rather than full shell script text.

## Assumptions

- AWF workspace containers are Linux containers with `/proc` mounted.
- Current in-container execution paths use the `agent` service and `/workspace` workdir.
- `docker compose exec -T` can run a cleanup shell after the original exec client is killed unless the container/stack is already gone.
- Cleanup failure during timeout/cancellation is an infrastructure failure, not an agent or validation failure.
- Operator cancellation or destroy status must take precedence over validation fix-pass continuation.

## Explicit Non-goals

- Do not use broad process-name cleanup such as `pkill claude`, `pkill codex`, `pkill pytest`, or `killall`.
- Do not change branch management, pushing, PR creation, merge policy, or monitor review-grace semantics.
- Do not hide cleanup failures behind retries or remap them to ordinary validation failures.
- Do not reduce coverage thresholds, skip tests, xfail tests, or loosen quality gates.
- Do not change profile schema unless implementation proves an existing field is insufficient.
- Do not attempt to defend against intentionally malicious daemonization beyond targeted process-group and invocation-id cleanup.
