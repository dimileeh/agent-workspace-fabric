# PR301 Full Coverage Gap Validation

Plan reference: `plans/PR301_FULL_COVERAGE_GAP_PLAN.md`

## Requirement Status

- Preserve the existing `99%` coverage requirement; do not skip, disable, or
  weaken CI: Complete.
- Do not edit protected GitHub workflow or quality-gate configuration files:
  Complete.
- Add focused regression coverage for uncovered worker branches introduced or
  exposed by this PR: Complete.
- Keep validation local and narrow; AWF/GitHub owns full coverage and broad CI
  after this agent phase: Complete.
- Commit the fix locally on the current AWF-managed branch: Complete; this
  validation is included in the local fix commit.

## Evidence

Files changed:

- `tests/unit/control/test_worker_scheduler_admission.py`
- `plans/PR301_FULL_COVERAGE_GAP_PLAN.md`
- `plans/PR301_FULL_COVERAGE_GAP_VALIDATION.md`

CI failure evidence used:

- GitHub Actions run `26606993152`, job `python-full-coverage`, reported
  `8303 passed` and coverage `98.99%`, below `--cov-fail-under=99`.
- Downloaded `full-coverage-report` artifact showed uncovered worker admission,
  claim, and execution-task branches in `src/awf/control/worker/admission.py`,
  `src/awf/control/worker/claims.py`, and
  `src/awf/control/worker/manager.py`.

Focused verification run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q`
  - Result: `24 passed in 23.09s`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py --cov=awf.control.worker.admission --cov=awf.control.worker.claims --cov=awf.control.worker.manager --cov-report=term-missing --cov-fail-under=0 -q`
  - Result: `24 passed in 25.72s`
  - Focused report showed `src/awf/control/worker/admission.py` at `100.00%`
    and no longer listed the targeted uncovered `claims.py` and `manager.py`
    lines from the CI artifact.
- `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_worker_scheduler_admission.py`
  - Result: `All checks passed!`
- `uv run --python 3.12 --extra dev ruff format --check tests/unit/control/test_worker_scheduler_admission.py`
  - Result: `1 file already formatted`

Full repository coverage and required CI checks were not run locally because
the AWF workspace contract assigns broad coverage, provenance, and merge gates
to AWF/GitHub after agent completion.

## Gaps

No implementation gaps remain for the saved plan.
