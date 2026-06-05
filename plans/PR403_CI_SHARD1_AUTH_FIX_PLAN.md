# PR403 CI Shard 1 Auth Fix Plan

## Problem

GitHub CI shard 1 failed
`TestWorkspaceObservability.test_runtime_fetches_without_token_header_when_unset`.
The current branch makes `_api_token_headers()` fall back to
`local_service_environ()` and therefore sends `Bearer local-dev-token` for
ordinary CLI API calls when `AWF_API_TOKEN` is unset.

That is broader than the source-checkout Compose contract requires and can leak
the known local development token to explicit non-local API targets.

## Plan

- Keep root Compose and service-runtime env resolution defaults unchanged.
- Revert generic CLI API header resolution to explicit `--api-token` or process
  `AWF_API_TOKEN` only.
- Leave service-aware commands that intentionally resolve local `.env` values
  responsible for passing their resolved token explicitly.
- Update the newer tests that expected global local-token fallback.
- Re-run the exact failed CI test, the touched CLI helper tests, and the env
  parser tests from the review-comment fixes before pushing.
