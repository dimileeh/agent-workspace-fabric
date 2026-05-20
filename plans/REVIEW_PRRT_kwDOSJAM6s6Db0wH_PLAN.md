# REVIEW_PRRT_kwDOSJAM6s6Db0wH Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6Db0wH` reports that `collect_doctor_report`
does not distinguish an omitted `compose_env_file` from an explicit
`compose_env_file=None`. That causes doctor diagnostics to rediscover an
adjacent Compose `.env` even after a caller intentionally rejected that env
file as untrusted.

Scope is limited to preserving explicit null compose env selection for doctor
diagnostics and helper layers that forward doctor collection context.

## Requirements Checklist

- Add a regression test proving explicit `compose_env_file=None` does not load
  or pass an adjacent Compose `.env`.
- Preserve existing behavior where omitting `compose_env_file` still discovers
  a local Compose `.env`.
- Ensure doctor worker diagnostics omit `--env-file` when explicit null is
  provided.
- Ensure CLI-facing support-bundle/readiness helper paths can forward explicit
  null without accidentally turning it into omission.
- Keep changes small and aligned with existing service helper patterns.

## Implementation Steps

1. Add a focused doctor regression test for explicit null env-file selection.
2. Run the new/focused test to confirm it fails on current behavior.
3. Introduce a private sentinel for omitted `compose_env_file` in
   `awf.service.doctor`.
4. Update support bundle and readiness forwarding only as needed to preserve
   explicit null semantics.
5. Run focused unit tests, then lint/type checks for touched areas.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_doctor.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py tests/unit/service/test_readiness.py -q`
  passes if helper forwarding changes are needed.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/service/test_doctor.py tests/unit/service/test_support_bundle.py tests/unit/service/test_readiness.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes.
