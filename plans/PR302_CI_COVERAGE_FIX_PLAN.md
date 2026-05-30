# PR302 CI Coverage Fix Plan

## Problem Statement And Scope

PR #302 fails the required `python-full-coverage` GitHub Actions job because
the full test suite passes but aggregate Python coverage is 98.96%, below the
99% gate. CI points to uncovered branches in the first-run host setup rendering
slice, primarily `src/awf/host_setup/rendering.py`, with a few uncovered error
branches in `src/awf/host_setup/config.py`.

Scope is limited to adding focused regression coverage for the existing
first-run rendering/config behavior. Do not edit protected CI or quality-gate
configuration, do not weaken coverage enforcement, do not push, and do not run
broad AWF/GitHub-owned validation locally.

## Requirements Checklist

- Add targeted tests for the uncovered first-run rendering paths reported by
  CI: unknown reason code, missing/irregular issue shapes, provider-ref set JSON
  coercion, provider-ref-key redaction, reason/severity/remediation detail
  omission branches, and empty nested sequence pretty rendering.
- Add targeted tests for uncovered host setup config branches: non-mapping YAML
  constructor guard, unhashable YAML mapping keys, and write-time validation
  secret classification.
- Preserve existing behavior and public output contracts.
- Record focused verification evidence only; leave the broad full-coverage gate
  to AWF/GitHub after agent completion.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Add tests in `tests/unit/service/test_host_setup_rendering.py` for the
   specific uncovered rendering paths from the CI coverage table.
2. Add tests in `tests/unit/service/test_host_setup_config.py` for the specific
   uncovered config error branches from the CI coverage table.
3. Run focused tests for the edited test files, including a focused coverage
   report for `awf.host_setup.rendering` and `awf.host_setup.config`.
4. If tests expose a behavior bug, make the smallest production fix and rerun
   the same focused checks.
5. Create `plans/PR302_CI_COVERAGE_FIX_VALIDATION.md` with requirement status
   and command evidence.
6. Commit the plan, validation, and test changes locally.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py tests/unit/service/test_host_setup_config.py -q`
  - Passes all focused rendering/config tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py tests/unit/service/test_host_setup_config.py --cov=awf.host_setup.rendering --cov=awf.host_setup.config --cov-report=term-missing -q`
  - Passes and shows the previously uncovered lines covered in the focused
    module report.

Full repository coverage and required CI rollup remain AWF/GitHub-owned
validation after this agent phase.
