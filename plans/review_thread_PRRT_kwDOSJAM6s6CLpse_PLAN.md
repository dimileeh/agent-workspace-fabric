# Review Thread PRRT_kwDOSJAM6s6CLpse Plan

## Problem Statement And Scope

PR review reports that workspace create/read CLI commands do not send API token
headers after `/v1/workspaces` and `/v2/workspaces` became protected by
`require_api_token`. The scope is limited to the affected workspace CLI
commands and their surface metadata/tests.

## Requirements Checklist

- `awf workspace create` must read `AWF_API_TOKEN` by default and send
  `Authorization: Bearer <token>`.
- `awf workspace create` must expose `--api-token` and let it override the
  environment token while preserving `Idempotency-Key`.
- `awf workspace show` must read `AWF_API_TOKEN` by default and expose
  `--api-token`.
- `awf workspace list` must read `AWF_API_TOKEN` by default and expose
  `--api-token`.
- Protected CLI contract metadata must include `--api-token` for the affected
  registered commands.
- API tokens must not be printed in stdout or stderr.

## Implementation Steps

1. Add CLI regression tests covering API token headers for create, show, and
   list.
2. Update `src/awf/cli/main.py` to add `api_token` options and pass
   `_api_token_headers(api_token)` into the affected `_call` invocations.
3. Update contract capability metadata for affected CLI commands.
4. Run narrow CLI/contract tests, then broader relevant validation.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_auth_failure_alignment.py tests/unit/contracts/test_surface_metadata_alignment.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_cli.py tests/unit/contracts/_capabilities.py`
  passes.
