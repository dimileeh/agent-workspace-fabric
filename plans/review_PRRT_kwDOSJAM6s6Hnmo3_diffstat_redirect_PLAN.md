# Plan — PR #443 thread PRRT_kwDOSJAM6s6Hnmo3 (Bitbucket diffstat redirect)

## Reviewer claim
`src/awf/common/bitbucket_client.py:351` — the PR `diffstat` fetch goes through the
shared httpx client, which has `follow_redirects=False` (httpx default, and
`from_env` never enables it). Bitbucket Cloud documents the PR diffstat URL as a
302 redirect to the repo-level diffstat resource. So the 302 body is parsed as
non-JSON (empty → `None` → empty `values`) and `PRStatus.changed_paths` becomes
empty; the monitor's scope-policy check can then miss out-of-scope changes.

## Verdict
VALID. `from_env` (line 250) builds `httpx.AsyncClient(...)` with no
`follow_redirects=True`, and `_request` never inspects redirect responses, so any
3xx with a body that isn't JSON silently yields empty results (or raises).

## Fix (minimal, security-consistent)
Follow redirects at the single `_request` chokepoint, reusing the existing
same-origin SSRF guard already applied to pagination `next` links:
1. Extract the origin check from `_validate_next_url` into a shared
   `_assert_forge_origin(url, operation, *, what)` (keep the `next` message text
   so existing tests pass).
2. In `_request`, after the 429/backoff loop, if `response.is_redirect` and a
   `Location` header is present: validate the Location origin, then re-issue the
   request against the Location (bounded by `max_redirects`). A foreign-host
   Location is rejected *before* re-issuing (no Authorization leak / SSRF).
3. Add `_DEFAULT_MAX_REDIRECTS` + a `max_redirects` ctor param (parity with
   `max_pages`/`max_retries`); exceeding it raises a diagnosable
   `BITBUCKET_API_ERROR`.

## Tests (focused, TDD)
- `_request` follows a same-host 302 and returns the final JSON body.
- `_paginate` over a redirected diffstat collects the resolved page values.
- A redirect Location to a foreign host raises `BITBUCKET_API_ERROR` before the
  second request is issued (SSRF guard).
- Exceeding `max_redirects` raises `BITBUCKET_API_ERROR`.
- `fetch_pr_status` populates `changed_paths` from a redirected diffstat.

Broad AWF/CI validation (full suite, coverage gate) is owned by AWF after the
agent phase; only focused tests are run here.
