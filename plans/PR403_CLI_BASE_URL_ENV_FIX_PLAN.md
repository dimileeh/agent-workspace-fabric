# PR403 CLI Base URL Env Fix Plan

## Problem

The PR review thread correctly points out that general CLI commands can now
discover the local Compose API token from root `.env`, but `_base_url()` still
only reads `AWF_API_HOST_PORT` from the process environment. If the API host
port override exists only in root `.env`, the CLI sends an authorized request to
the wrong default port (`localhost:8000`).

## Plan

- Preserve existing precedence:
  - explicit `--base-url`,
  - `AWF_BASE_URL`,
  - deprecated `AWF_CLI_BASE_URL`,
  - shell `AWF_API_HOST_PORT`.
- Add root/local Compose env fallback for `AWF_API_HOST_PORT` via
  `local_service_environ(os.environ)`.
- Add regression coverage in the existing base URL resolution tests.
- Validate with the focused base URL tests, common helper tests, shard 2 shape
  if practical, lint/format/mypy, then commit and push.
