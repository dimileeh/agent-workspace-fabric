# Companion Env Secret Resume Precheck Plan

## Problem Statement

PR review comment `issue:4561562913` points out that monitor resume required-secret prechecks currently raise `ComposeOperationError`, so logs identify an AWF-level guard failure as `executor.resume_compose_up_failed`. The reason code is preserved, but the event name can misdirect operators toward Docker Compose.

## Scope

- Keep the existing resume behavior: missing or empty required companion env secrets should skip `ensure_project_up`, record monitor runtime restart failure context, and still allow the PR monitor resume flow to continue.
- Distinguish AWF precheck failures from Docker Compose subprocess failures in logs and persisted event payloads.
- Keep raw secret values out of logs, events, and compose YAML.
- Avoid broad validation; run only targeted tests for the touched executor resume behavior.

## Requirements Checklist

- Add a dedicated exception type for resume companion env-secret precheck failures.
- Raise that type from `_precheck_required_companion_env_secrets_for_resume`.
- Catch/log that type with a dedicated event name, separate from actual Compose failures.
- Preserve existing reason codes and diagnostic stderr fields.
- Add or update a focused regression test that fails before the implementation and proves the dedicated log/event path.
- Document focused validation evidence in a validation file.

## Implementation Steps

1. Add the failing regression assertion around `resume_pr_monitor` required-secret precheck logging and event payload.
2. Run the targeted regression test to confirm the current behavior fails.
3. Implement the dedicated exception type and resume catch path.
4. Re-run the targeted test file or selected tests that cover the changed behavior.
5. Write `plans/COMPANION_ENV_SECRET_RESUME_PRECHECK_VALIDATION.md` with requirement status and evidence.
6. Stage only changed files and commit locally with a review-comment-specific conventional commit message.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -q -k "required_companion_env_secret_reason_code or compose_failure_records_warning"`

Pass criteria: selected tests pass and demonstrate that required-secret precheck failures use the dedicated AWF precheck event while real Compose failures still use the compose failure path.
