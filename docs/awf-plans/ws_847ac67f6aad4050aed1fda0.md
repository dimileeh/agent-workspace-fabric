# Issue 346 Plan: PR-Monitor Pre-Push Toolchain Setup and 127 Classification

## Scope

Resolve GitHub issue #346 by making PR-monitor pre-push validation run with the same profile setup assumptions as the normal agent/validate path, and by classifying command-not-found failures (`returncode == 127`) as a distinct toolchain-missing condition instead of spending a validation fix pass.

This plan is for implementation only. The planning phase may touch only this artifact; the implementation phase must create `plans/ISSUE_346_PLAN.md` before code edits and `plans/ISSUE_346_VALIDATION.md` after validation, per `plans/PLAN_EXECUTION_PROTOCOL.md`.

## Current Seams Observed

- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
  - `_pre_push_validation_commands()` currently builds the fix prompt command list from `post_agent` and `validate` only.
  - `_run_pre_push_validation_with_fix_passes()` consumes fix-pass budget for any ordinary pre-push validation failure with a first failure.
  - `_run_pre_push_validation()` calls `ValidationRunner.run_profile_phases(..., phase_names=("post_agent", "validate"))`.
  - `_PrePushValidationResult.failure_details()` currently includes high-level reason data but not the failing command or return code.
- `src/awf/runtime/pr_monitor_runner/pre_push_validation_constants.py`
  - Defines existing pre-push reason codes; it needs a new `PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING` reason.
- `src/awf/runtime/validation_runner.py` and `src/awf/runtime/validation_setup.py`
  - Own setup phase execution and ordering via `profile_phase_command_plan()`.
  - Validation command execution already activates workspace-local `.venv` before each command.
- `src/awf/control/executor/execution_flow.py` and `src/awf/control/executor/monitor_handoff.py`
  - Initial executor flow already runs `setup`/`pre_agent` before the normal task body; implementation must verify whether every monitor adoption/recovery path reaches that setup before pre-push validation.

## Intended Files and Modules to Touch

- `plans/ISSUE_346_PLAN.md`: implementation-phase saved plan required by repository protocol.
- `plans/ISSUE_346_VALIDATION.md`: implementation-phase validation record required by repository protocol.
- `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`: primary regression tests for 127 classification, fix-pass behavior, precedence, and failure-detail payloads.
- One focused executor/adoption/recovery test file, chosen after implementation-phase inspection of the exact setup seam:
  - likely `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_005.py` for adopted `sync_feature_pr` handoff setup behavior, or
  - likely `tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_002.py` for monitor recovery/validate-only setup reuse.
- `src/awf/runtime/pr_monitor_runner/pre_push_validation_constants.py`: add the new reason-code constant.
- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`: add toolchain-missing classification, fix-pass bypass, failure-detail enrichment, and any pre-push setup orchestration needed at this seam.
- Potentially `src/awf/control/executor/execution_flow.py` and/or `src/awf/control/executor/monitor_handoff.py`: only if implementation-phase investigation confirms the monitor adoption/provision/recovery path fails to run the existing setup phase before monitor handoff.
- Potentially `src/awf/common/audit.py` or `src/awf/common/redaction.py`: no planned behavior changes; use existing redaction helpers from these modules for command/detail payloads if needed.

## Tests to Write First

1. Pure 127 toolchain-missing classification:
   - Arrange a pre-push validation result whose validate command fails with `returncode=127` and an underlying validation reason such as `COMMAND_FAILED`.
   - Assert `_validated_git_push_result()` returns `reason_code == PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING`.
   - Assert the adapter/fix pass is not invoked, `_commit_dirty_worktree` is not invoked, and validation is not retried as a fix-pass cycle.
   - Assert no `PRE_PUSH_VALIDATION_FIX_FAILED` reason is surfaced.

2. Genuine lint/type failure still consumes the fix pass:
   - Arrange a validate command failure with `returncode=1` and findings/evidence.
   - Assert the existing fix-pass path still runs the repair agent and commit hook when budget is available.
   - Assert a failed repair still surfaces `PRE_PUSH_VALIDATION_FIX_FAILED`, preserving existing behavior.

3. Precedence when both 127 and real failures are present:
   - Build a `ValidationResult` containing both a 127 command and a real non-zero validation failure.
   - Assert the real validation failure takes precedence over toolchain-missing classification and remains eligible for the fix-pass path.
   - Document in code/tests that pure 127 means all actionable failures are command-not-found/toolchain-missing; mixed failures surface the real validation failure.

4. Failure-detail payload diagnostics:
   - Assert `_GitPushResult.details` for `PRE_PUSH_VALIDATION_FIX_FAILED` includes the failing command and return code.
   - Assert `_GitPushResult.details` for `PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING` includes the failing command and return code.
   - Ensure command text is passed through existing audit/redaction helpers before persistence.

5. Setup/toolchain path coverage:
   - First inspect whether adopted monitor workspaces already execute `setup` at provision/handoff or whether only `post_agent`/`validate` run in monitor pre-push.
   - If setup is missing at provision/handoff: add a test proving the adopted PR monitor setup path invokes `ValidationRunner.run_profile_phases(..., phase_names=("setup", "pre_agent"), ...)` before entering monitor pre-push, and that setup failure blocks handoff with the existing setup-failure handling.
   - If setup already runs but pre-push loses the setup-created environment: add a focused pre-push test proving the pre-push validation path reuses the existing `ValidationRunner` environment behavior rather than a custom setup mechanism.
   - The implementation must not run install-heavy setup on every comment-repair/pre-push cycle.

## Implementation Steps

1. Create `plans/ISSUE_346_PLAN.md` from this plan before code edits.
2. Add failing unit tests in the order above and confirm at least the new 127/setup tests fail before implementation when practical.
3. Add `_PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING_REASON = "PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING"` and public module alias alongside existing pre-push constants.
4. Add small helper logic in `pre_push_validation.py` to inspect validation command results:
   - collect failed commands;
   - identify `returncode == 127` failures;
   - identify real validation failures as failed commands whose return code is not 127;
   - classify as toolchain-missing only when failures are purely 127;
   - prefer the real failure if both 127 and non-127 failures are present.
5. Update `_run_pre_push_validation_with_fix_passes()` so toolchain-missing results return immediately without invoking `_run_pre_push_validation_fix_pass()` or consuming fix-pass budget.
6. Update `_PrePushValidationResult.failure_details()` to include a redacted failing command and integer return code when `first_failure` exists. Keep existing keys stable.
7. Implement the setup/toolchain availability fix at the narrowest verified seam:
   - Prefer monitor adoption/provision/handoff setup once, using `ValidationRunner.run_profile_phases()` and the existing `setup` phase machinery.
   - If the monitor path already runs setup, fix the environment reuse/persistence path instead of adding repeated setup to pre-push validation.
   - Keep setup idempotent and generic across Python, Node, and other profile commands.
8. Update the saved implementation plan if investigation changes the exact setup seam.
9. Add `plans/ISSUE_346_VALIDATION.md` after implementation with requirement status and evidence.

## Validation Commands

Planning phase: no validation commands are run.

Implementation-phase focused checks, in order:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q
uv run --python 3.12 --extra dev pytest <focused executor/adoption/recovery test file> -q
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/pre_push_validation_constants.py <any touched executor file> tests/unit/runtime/test_pr_monitor_pre_push_validation.py <focused touched test file>
uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py
```

If the implementation touches executor handoff/recovery typing-sensitive paths, also run a focused mypy check on the touched executor module.

Per the active AWF workspace contract, avoid running full-repository suites, coverage gates, frontend builds, or CI-equivalent validation during the agent phase unless the operator explicitly re-authorizes that broad diagnostic action. Record that AWF/GitHub own broad post-agent validation and merge gating.

## Risks and Assumptions

- Assumption: `returncode == 127` from profile validation is a reliable command-not-found/toolchain-unavailable signal for this bug class.
- Assumption: `ValidationRunner.run_profile_phases()` is the correct reusable setup mechanism; no PR-monitor-specific installer should be added.
- Risk: Initial adoption/handoff may already run setup, while long-lived monitor recovery or preserved workspace resume may bypass it. The setup test must target the actual missing path after inspection.
- Risk: Running `setup` inside every pre-push validation loop would fix the symptom but add expensive repeated dependency installs. The implementation should avoid that unless no one-time setup seam exists, and then must persist/cache enough state to keep repeated cycles cheap.
- Risk: Failure-detail payloads may contain shell commands with embedded secret-like values. Use existing audit/redaction helpers and keep payload values bounded.
- Risk: `ValidationRunner` normally stops after the first required command failure, so the mixed 127/non-127 precedence test may use a synthetic `ValidationResult`; document the classification contract clearly.
- Risk: Reason-code propagation may touch event or operation payloads indirectly through `_GitPushResult.details`; verify existing callers do not assume only the three old reason codes.

## Explicit Non-Goals

- Do not introduce project-specific Python, ruff, npm, eslint, or Aira assumptions into AWF core.
- Do not build a parallel setup/install mechanism outside `ValidationRunner` and profile phases.
- Do not change merge policy, review-grace behavior, stale detection, or PR comment resolution semantics.
- Do not run dependency installation on every monitor comment-repair cycle.
- Do not regenerate OpenAPI unless schema changes are discovered, which is not expected.
- Do not switch branches, push, rebase, force-push, or commit during this workspace task.
