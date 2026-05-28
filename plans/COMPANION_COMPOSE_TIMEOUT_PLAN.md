# Companion Compose-Up Timeout Plan

## Summary

Fix issue #291 by replacing the fixed 360 second `docker compose up`
subprocess ceiling with a configurable timeout path for managed companion
services. Fold the change into PR #292 on `codex/companion-env-secrets`.

## Implementation

- Add `compose_up_timeout_seconds: int | None = None` to companion requests,
  with validation bounds of 1 to 1800 seconds.
- Persist the field through workspace task policy and parse it into
  `WorkspaceCompanionSpec`.
- Compute the effective compose wait timeout as the maximum of
  `profile.docker.startup_timeout_seconds` and all companion timeout overrides.
- Pass that effective timeout into `WorkspaceComposeSpec`.
- Render `docker compose up --wait-timeout <effective>` and run the compose
  subprocess with a capture timeout of `<effective + 60>`.
- Keep existing default behavior: profile default 300 seconds plus 60 seconds
  capture buffer remains 360 seconds.
- Expose the field through REST/OpenAPI, MCP companion objects, and CLI
  `--companion-json` documentation.

## Tests

- API/schema tests accept valid values, reject out-of-range values, and expose
  OpenAPI bounds.
- CLI/MCP tests prove companion JSON/object payloads can carry the timeout
  through canonical workspace create validation.
- Runtime tests prove task policy persistence, task policy parsing, profile
  timeout fallback, companion override, profile override, and multi-companion
  max behavior.
- Compose tests prove `--wait-timeout`, subprocess capture timeout, and timeout
  error text use the effective values.

## Validation

- Targeted pytest for schema, CLI/MCP creation, companion services, stack
  launcher, and compose manager tests.
- Ruff and mypy on touched Python files.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`.
