# PR403 Local CLI Token Fallback Plan

## Problem

The latest PR review correctly notes that a fresh source checkout can start the
API with `docker compose up --build`, where Compose supplies
`AWF_API_TOKEN=local-dev-token`. Generic CLI calls currently do not discover
that local default, so commands such as `awf workspace list` can 401 unless the
operator manually exports `AWF_API_TOKEN`.

The fallback must stay narrow: it should help implicit/local CLI targets, not
send `local-dev-token` to arbitrary remote `--base-url` targets.

## Plan

- Add a regression proving an implicit local CLI call uses the local Compose
  token when no explicit token is set.
- Add a regression proving an explicit remote base URL does not receive the
  local Compose token.
- Keep `_api_token_headers()` explicit-only.
- Teach `_call()` to add the local Compose token only when the resolved target
  URL is loopback and no Authorization header is already present.
- Validate focused CLI tests, lint/format, and mypy.
