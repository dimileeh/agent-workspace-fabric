# COMMENT_4552714190_FOLLOWUP_PLAN

## Problem statement and scope
Address the two actionable review issues from PR comment `issue:4552714190`:

- The console Playwright CI wrapper drops unknown install flags while rewriting
  `install chromium` to `install --only-shell chromium`.
- PR-monitor pre-push validation can persist an invisible validation run with
  `attempt_id=None` when a monitoring workspace has no task attempt.

Scope is limited to the wrapper normalization behavior, the PR monitor
pre-push validation startup guard, and targeted regression tests.

## Requirements
- Preserve unknown Playwright install flags when normalizing CI Chromium
  installs.
- Keep existing known flag behavior and non-CI/non-Chromium behavior unchanged.
- Fail PR-monitor pre-push validation explicitly when no task attempt exists
  instead of creating a validation run with `attempt_id=None`.
- Do not invoke the validation runner, raw push, or persist a validation run in
  that no-attempt infrastructure failure case.
- Use focused tests only; broad AWF/GitHub validation remains owned by AWF after
  agent completion.

## Implementation steps
1. Add a console wrapper unit test showing unknown install flags survive CI
   Chromium normalization.
2. Add a pre-push validation unit test for a monitoring workspace without a task
   attempt, asserting an infrastructure failure and no persisted run.
3. Update `playwright-ci-wrapper.cjs` to pass all flag arguments through when
   adding `--only-shell`.
4. Update pre-push validation run startup to return no run id when no task
   attempt exists, and have the caller fail fast with an infrastructure reason.
5. Run the two focused test commands that cover these changes.

## Verification commands
- `node --test apps/console/lib/playwright-ci-wrapper.test.mjs`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q`

Pass criteria:
- The wrapper unit tests pass with unknown flags preserved.
- The pre-push validation unit tests pass, including the no-attempt
  infrastructure failure regression.
