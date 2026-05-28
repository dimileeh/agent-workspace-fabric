# CI Exact Coverage Fix Validation

Plan reference: `plans/CI_EXACT_COVERAGE_FIX_PLAN.md`

## Requirement Status

- Complete: Preserve the AWF-managed branch and do not push.
  - Stayed on `awf/ws_a829bac6193d48be8b2a4f14`; no push/rebase/branch switch.
- Complete: Do not edit protected workflow, quality-gate, or configuration
  files.
  - Changed only a focused unit test plus plan/validation documents.
- Complete: Add focused regression coverage for real PR behavior.
  - Added
    `test_init_bootstrap_helper_rejects_unknown_provider_before_local_setup`
    in `tests/unit/cli/test_init_parts/test_init_part_002.py`.
  - The test exercises the preserved private init bootstrap helper's unknown
    provider error path and asserts provider validation happens before Compose
    path discovery.
- Complete: Run focused local checks only.
  - Full repository coverage, full unit suite, and CI-equivalent validation
    were not run locally; AWF/GitHub own those gates after agent completion.
- Complete: Record evidence in this validation document.
- Complete: Commit locally.
  - Pending at validation-write time; commit will include this document.

## Evidence

- CI failure inspected from GitHub Actions run `26606065559`:
  - `python-full-coverage` failed only in `Enforce exact coverage threshold`.
  - Reported combined coverage was `98.9999%`, with `55140/55697` combined
    line+branch opportunities covered.
  - One additional covered opportunity would raise the displayed combined
    coverage above `99.00%`; this test covers three previously uncovered
    `awf.cli.init_ops` lines from the CI artifact (`1174-1176`).
- Downloaded and inspected the CI `full-coverage-report` artifact locally under
  `/tmp/awf-ci-coverage`.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_002.py -q`
  - Passed: `39 passed in 1.11s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_002.py --cov=awf.cli.init_ops --cov-report=term-missing --cov-fail-under=0 -q`
  - Passed: `39 passed in 1.87s`.
  - The focused missing-lines report no longer lists `1174-1176`.
- `uv run --python 3.12 --extra dev ruff check tests/unit/cli/test_init_parts/test_init_part_002.py`
  - Passed: `All checks passed!`

## Notes

- An earlier focused coverage probe without `--cov-fail-under=0` ran the same
  targeted test file successfully but exited non-zero because the repository's
  global `fail_under=99` applies even to single-module coverage probes. The
  plan was updated to keep the local probe non-gating and avoid substituting it
  for AWF/GitHub's broad exact coverage gate.
