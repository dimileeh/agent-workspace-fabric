# Review Thread PRRT_kwDOSJAM6s6CLpse Validation

Plan reference: `plans/review_thread_PRRT_kwDOSJAM6s6CLpse_PLAN.md`

## Requirement Status

- Complete: `awf workspace create` reads `AWF_API_TOKEN` by default and sends
  `Authorization: Bearer <token>`.
- Complete: `awf workspace create` exposes `--api-token`, overrides the
  environment token, and preserves `Idempotency-Key`.
- Complete: `awf workspace show` reads `AWF_API_TOKEN` by default and exposes
  `--api-token`.
- Complete: `awf workspace list` reads `AWF_API_TOKEN` by default and exposes
  `--api-token`.
- Complete: protected CLI contract metadata includes `--api-token` for the
  affected registered commands.
- Complete: regression tests assert API tokens are not printed in stdout or
  stderr.

## Evidence

Files changed:

- `src/awf/cli/main.py`
- `tests/unit/cli/test_cli.py`
- `tests/unit/contracts/_capabilities.py`
- `plans/review_thread_PRRT_kwDOSJAM6s6CLpse_PLAN.md`
- `plans/review_thread_PRRT_kwDOSJAM6s6CLpse_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py::TestWorkspaceCreate::test_api_token_header_forwarded_without_printing_secret tests/unit/cli/test_cli.py::TestWorkspaceCreate::test_api_token_option_overrides_env_token tests/unit/cli/test_cli.py::TestWorkspaceShow::test_injects_env_api_token_without_printing_it tests/unit/cli/test_cli.py::TestWorkspaceShow::test_api_token_option_overrides_env_token tests/unit/cli/test_cli.py::TestWorkspaceList::test_injects_env_api_token_without_printing_it tests/unit/cli/test_cli.py::TestWorkspaceList::test_api_token_option_overrides_env_token -q`
  - Initial run before implementation failed for missing headers/options.
  - Final run passed: `6 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py -q`
  passed: `94 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_auth_failure_alignment.py tests/unit/contracts/test_surface_metadata_alignment.py -q`
  passed: `175 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_cli.py tests/unit/contracts/_capabilities.py`
  passed.

## Gaps

No gaps remain.
