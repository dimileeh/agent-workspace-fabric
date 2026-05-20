# PRRT_DXD4Q Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6DXD4Q` reports that appended workflow
validation commands accept arbitrary `python tests/...` scripts. The scope is
limited to the protected workflow quality-gate logic and unit tests.

## Requirements Checklist

- Add a regression test proving an appended `python tests/...` script is blocked.
- Preserve allowed appended validation runner forms such as explicit `python -m`
  validation modules.
- Keep the change narrow to `src/awf/control/quality_gates.py` and related tests.
- Run the narrow unit tests that prove the regression and surrounding behavior.

## Implementation Steps

1. Add a failing unit test for `coverage xml && python tests/exfiltrate.py`.
2. Restrict appended Python validation commands to explicit module-runner forms.
3. Re-run the focused quality-gate tests.
4. Record validation evidence in `plans/PRRT_DXD4Q_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
