# REVIEW_4496235802_NO_WORK_PARSE_DIAGNOSTICS Plan

## Problem Statement And Scope

PR review comment `issue:4496235802` still has two actionable defensive
recovery gaps:

- preserved-active `no_work` recovery only waits for grace for an explicit
  reason allowlist, so future `no_work` reasons could create a replacement
  immediately;
- all-fail open-PR lookup parsing raises the first item error without aggregate
  failure context.

Scope is limited to the preserved-active `no_work` grace gate, open-PR lookup
diagnostics, focused regressions, and this plan/validation record.

## Assumptions/Changes

During broader touched-file validation, two existing worker regressions
reproduced independently and align with the review-level concern about central
preserved-active/stale-active orchestration:

- stale-active scans can inspect runtime after a transient closed database
  connection instead of aborting the candidate before Docker/runtime inspection;
- preserved-active validation-requested recovery can deadlock by recording a
  blocked event from a helper while the outer recovery session still holds the
  workspace row lock.

The plan scope now includes minimal fixes for those two regressions, using the
existing failing tests as regression coverage.

## Requirements Checklist

- Add a regression proving an unknown future `no_work` classification reason is
  blocked during preservation grace instead of creating a replacement.
- Change the `no_work` recovery branch so all non-expired `no_work`
  classifications honor preservation grace.
- Add a regression proving all-fail open-PR parsing exposes aggregate failure
  context.
- Preserve partial-success open-PR parsing behavior: malformed items log while
  parseable items are returned.
- Preserve the existing stale-active closed-connection regression: a transient
  DB failure aborts the candidate before runtime inspection and records the DB
  transient event.
- Preserve the existing validation-requested/no-executor regression: recovery
  records `validation_executor_unavailable` without hanging while the workspace
  remains non-terminal.
- Validate with focused unit tests and the narrowest lint/type checks practical
  for touched files.

## Implementation Steps

1. Add focused failing tests in `tests/unit/control/test_worker.py` and
   `tests/unit/common/test_github_client.py`.
2. Run the focused new tests to confirm the current behavior fails.
3. Update `src/awf/control/worker.py` to block every non-expired `no_work`
   classification.
4. Update `src/awf/common/github_client.py` so all-fail parse errors include
   total count and per-item reason summaries.
5. Update stale-active recovery to verify the candidate through the database
   before runtime inspection and release the preserved-active recovery session
   before recording validation-unavailable blocked salvage.
6. Run focused tests, then targeted lint/type validation.
7. Record validation evidence in
   `plans/REVIEW_4496235802_NO_WORK_PARSE_DIAGNOSTICS_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::<targeted-test> -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py::<targeted-test> -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py tests/unit/common/test_github_client.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/common/github_client.py tests/unit/control/test_worker.py tests/unit/common/test_github_client.py`
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py src/awf/common/github_client.py`

Pass criteria: targeted regressions fail before implementation, all focused
tests pass after implementation, and lint/type checks pass or any environment
blocker is documented in validation.
