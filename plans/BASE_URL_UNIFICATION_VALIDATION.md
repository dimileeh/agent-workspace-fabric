# Base URL Unification Validation

Plan reference: `plans/BASE_URL_UNIFICATION_PLAN.md`

## Requirement Status

- Complete: Host CLI precedence is `--base-url` > `AWF_BASE_URL` >
  deprecated `AWF_CLI_BASE_URL` > `http://localhost:${AWF_API_HOST_PORT:-8000}`
  > `http://localhost:8000`.
- Complete: `AWF_API_HOST_PORT=8800` with no other host CLI URL now targets
  `http://localhost:8800`.
- Complete: `AWF_CLI_BASE_URL` remains supported and emits one deprecation
  notice per process when `AWF_BASE_URL` is absent.
- Complete: `AWF_BASE_URL` emits no deprecated-variable notice and wins over
  `AWF_CLI_BASE_URL`.
- Complete: `AWF_API_BASE_URL` / service `settings.api_base_url` behavior is
  unchanged and is covered against accidental `AWF_BASE_URL` coupling.
- Complete: Docs and examples present `AWF_BASE_URL` as the host/operator knob,
  mark `AWF_CLI_BASE_URL` deprecated, and explain `AWF_API_BASE_URL` as the
  service-side self-reference URL.
- Complete: Import-order test isolation was tightened after the requested full
  pytest gate exposed module-cache pollution unrelated to base URL behavior.

## Evidence

TDD failure before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestBaseUrlResolution -q
# 4 failed, 2 passed
```

Service no-regression tests were already consistent with existing behavior:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_config_parts/test_config_part_001.py -k "api_base_url or base_url" -q
# 29 passed, 58 deselected
```

Focused checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestBaseUrlResolution -q
# 6 passed

uv run --python 3.12 --extra dev pytest tests/unit/service/test_config_parts/test_config_part_001.py -k "api_base_url or base_url" -q
# 29 passed, 58 deselected

uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestBaseUrlResolution tests/unit/service/test_config_parts/test_config_part_001.py -k "api_base_url or base_url" -q
# 32 passed, 61 deselected

uv run --python 3.12 --extra dev ruff check src/awf/cli/common.py src/awf/cli/__init__.py src/awf/common/config.py src/awf/service/config.py tests/unit/cli/test_cli_parts/test_cli_part_002.py tests/unit/service/test_config_parts/test_config_part_001.py
# All checks passed

uv run --python 3.12 --extra dev mypy src/awf/cli/common.py src/awf/common/config.py src/awf/service/config.py
# Success: no issues found in 3 source files
```

Requested broad gate:

```bash
uv run --python 3.12 --extra dev ruff check .
# All checks passed

uv run --python 3.12 --extra dev ruff format --check .
# 907 files already formatted

uv run --python 3.12 --extra dev mypy
# Success: no issues found in 305 source files

uv run --python 3.12 --extra dev pytest
# First run exposed 4 order-sensitive failures in import-order/module-cache tests.
```

Import-order cleanup reproductions:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics_import_order.py tests/unit/service/test_metrics_parts/test_metrics_part_001.py::test_resource_saturation_reuses_allocation_auxiliary_counts_for_capacity_gate tests/unit/service/test_metrics_parts/test_metrics_part_002.py::test_capacity_queue_blocked_reason_counts_caps_provider_suppression_refill_pages -q
# 3 passed

uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_import_cycle.py tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py::test_retry_workspace_errors_and_missing_source_attempt_fallback -q
# 4 passed

uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_split_imports.py tests/unit/test_postgres_only_edges.py::test_adoption_head_repo_slug_validation_edges -q
# 3 passed
```

Final requested broad gate after import-order cleanup:

```bash
uv run --python 3.12 --extra dev ruff check .
# All checks passed

uv run --python 3.12 --extra dev ruff format --check .
# 907 files already formatted

uv run --python 3.12 --extra dev mypy
# Success: no issues found in 305 source files

uv run --python 3.12 --extra dev pytest
# 9288 passed, 7 skipped in 3351.63s
```

The 7 skips are Docker/Compose-dependent integration tests skipped because the
Docker daemon or Compose plugin is unavailable in this workspace.

## Files Changed

- CLI/config: `src/awf/cli/common.py`, `src/awf/cli/__init__.py`,
  `src/awf/common/config.py`, `src/awf/service/config.py`
- Tests: `tests/unit/cli/test_cli_parts/test_cli_part_002.py`,
  `tests/unit/service/test_config_parts/test_config_part_001.py`,
  `tests/unit/service/test_metrics_import_order.py`,
  `tests/unit/service/test_workspace_retry_import_cycle.py`,
  `tests/unit/common/test_github_client_split_imports.py`
- Docs/examples: `.env.example`, `docs/GETTING_STARTED.md`,
  `docs/CONCEPTS.md`, `docs/TROUBLESHOOTING.md`,
  `docs/CLI_REFERENCE.md`, `docs/PR_MONITOR_ADOPTION.md`

## Remaining Gaps

None.
