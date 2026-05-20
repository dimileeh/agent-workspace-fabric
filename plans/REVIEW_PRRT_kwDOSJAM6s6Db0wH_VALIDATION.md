# REVIEW_PRRT_kwDOSJAM6s6Db0wH Validation

Plan reference: `REVIEW_PRRT_kwDOSJAM6s6Db0wH_PLAN.md`

## Requirement Status

- Add a regression test proving explicit `compose_env_file=None` does not load
  or pass an adjacent Compose `.env`: Complete.
  Evidence: `tests/unit/service/test_doctor.py`.
- Preserve existing behavior where omitting `compose_env_file` still discovers
  a local Compose `.env`: Complete.
  Evidence: existing doctor env-file discovery tests remain green.
- Ensure doctor worker diagnostics omit `--env-file` when explicit null is
  provided: Complete.
  Evidence: `test_doctor_worker_inspection_honors_explicit_null_compose_env_file`.
- Ensure CLI-facing support-bundle/readiness helper paths can forward explicit
  null without accidentally turning it into omission: Complete.
  Evidence: support bundle forwarding and readiness provider-env regressions.
- Keep changes small and aligned with existing service helper patterns:
  Complete.
  Evidence: shared private sentinel in `awf.service.config`; no unrelated
  behavior changes.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_doctor.py::test_doctor_worker_inspection_honors_explicit_null_compose_env_file tests/unit/service/test_support_bundle.py::test_support_bundle_forwards_explicit_null_compose_env_file tests/unit/service/test_readiness.py::test_core_readiness_honors_explicit_null_compose_env_file -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_doctor.py tests/unit/service/test_support_bundle.py tests/unit/service/test_readiness.py tests/unit/service/test_status.py -q`
  passed: 133 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/service/test_doctor.py tests/unit/service/test_support_bundle.py tests/unit/service/test_readiness.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Notes

An additional broad `uv run --python 3.12 --extra dev pytest tests/unit -q`
attempt was stopped at roughly 11% because it was too slow for this narrow
review-thread cycle. The affected service suites above completed successfully.
