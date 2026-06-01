# COMMENT_3331002383 API Host Port Validation

Plan reference: `plans/COMMENT_3331002383_API_HOST_PORT_PLAN.md`

## Requirement Status

- Complete: Reject non-numeric `AWF_API_HOST_PORT` values before any HTTP
  request is made.
- Complete: Reject host ports outside the valid TCP port range `1..65535`.
- Complete: Preserve valid `AWF_API_HOST_PORT` behavior and existing precedence
  for `--base-url`, `AWF_BASE_URL`, and deprecated `AWF_CLI_BASE_URL`.
- Complete: Report a clear CLI error and exit with code `2` for invalid host
  ports.
- Complete: Use focused tests only; full AWF/GitHub validation remains managed
  by AWF after the agent phase.

## Evidence

- Changed `src/awf/cli/common.py` to parse and validate `AWF_API_HOST_PORT`
  before deriving the localhost API URL.
- Added regression coverage in
  `tests/unit/cli/test_cli_parts/test_cli_part_002.py`.
- Failing-before-fix check:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestBaseUrlResolution::test_invalid_api_host_port_exits_before_request -q`
  failed because the CLI continued into the HTTP request path.
- Passing checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestBaseUrlResolution::test_invalid_api_host_port_exits_before_request -q`
  passed with `3 passed`.
- Passing checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestBaseUrlResolution -q`
  passed with `9 passed`.
- Passing checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py -q`
  passed with `59 passed`.
- Passing checks:
  `uv run --python 3.12 --extra dev ruff check src/awf/cli/common.py tests/unit/cli/test_cli_parts/test_cli_part_002.py`
  passed.

## Gaps

None. Broad AWF/GitHub validation was intentionally not run during the agent
phase per the workspace contract.
