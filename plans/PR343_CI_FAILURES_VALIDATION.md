# PR #343 CI Failure Repair Validation

Plan reference: `plans/PR343_CI_FAILURES_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Keep AWF's current branch and do not push | Complete | Work stayed on the existing AWF branch; no push commands were run. |
| Update health/status readiness contract expectations to include Grok | Complete | `tests/unit/api/test_health_parts/test_health_part_001.py` and `tests/unit/service/test_status_parts/test_status_part_001.py` now include `grok` in provider readiness expectations. |
| Keep Grok provider readiness behavior intact | Complete | `src/awf/service/provider_readiness.py` still reports Grok readiness; helper extraction was mechanical and provider-readiness shards pass. |
| Split first-party code/test files under 1,500 lines | Complete | `provider_readiness.py` is 1,483 lines and `test_provider_readiness_part_001.py` is 1,488 lines; moved tail tests into part 003. |
| Run provided focused pytest repro | Complete | Focused failing nodes passed. |
| Record focused evidence only | Complete | Only targeted pytest, ruff, and mypy checks were run; full AWF/GitHub validation remains managed by AWF after agent completion. |

## Files Changed

- `src/awf/service/provider_readiness.py`
- `src/awf/service/provider_readiness_helpers.py`
- `tests/unit/api/test_health_parts/test_health_part_001.py`
- `tests/unit/service/test_status_parts/test_status_part_001.py`
- `tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py`
- `tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_003.py`

## Focused Evidence

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_001.py::test_readyz_response_shape_matches_contract tests/unit/service/test_status_parts/test_status_part_001.py::test_service_status_provider_warnings_do_not_fail_by_default tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
# 3 passed
```

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_health_parts/test_health_part_001.py::test_readyz_response_shape_matches_contract tests/unit/service/test_status_parts/test_status_part_001.py::test_service_status_provider_warnings_do_not_fail_by_default tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_003.py -q
# 8 passed
```

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts -q
# 111 passed
```

```bash
uv run --python 3.12 --extra dev ruff check src/awf/service/provider_readiness.py src/awf/service/provider_readiness_helpers.py tests/unit/api/test_health_parts/test_health_part_001.py tests/unit/service/test_status_parts/test_status_part_001.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_003.py
# All checks passed
```

```bash
uv run --python 3.12 --extra dev mypy src/awf/service/provider_readiness.py src/awf/service/provider_readiness_helpers.py
# Success: no issues found in 2 source files
```

No broad coverage or full repository validation was run locally, per the AWF
workspace contract.
