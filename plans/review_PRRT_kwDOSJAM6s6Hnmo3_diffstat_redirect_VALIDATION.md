# Validation — PR #443 thread PRRT_kwDOSJAM6s6Hnmo3 (Bitbucket diffstat redirect)

## Verdict: FIXED

The reviewer is correct. `from_env` builds the httpx client with the default
`follow_redirects=False` and `_request` never inspected 3xx responses, so the PR
`diffstat` 302 redirect collapsed `PRStatus.changed_paths` to empty — degrading
the monitor's scope-policy check on Bitbucket PRs.

## Change
- `_request` now follows same-host 3xx `Location` hops (bounded by
  `max_redirects`, default 5), re-issuing the authenticated GET against the
  resolved URL. Production `from_env` is intentionally left non-auto-following so
  every hop passes the SSRF origin guard first.
- SSRF guard extracted into shared `_assert_forge_origin(...)`, reused by both the
  pagination `next` check (unchanged message/behavior) and the new redirect path.
  A foreign-host `Location` is rejected *before* the request is re-issued, so the
  `Authorization` header never leaks.

## Focused checks (run in-workspace)
- `pytest tests/unit/common/test_bitbucket_client_parts tests/unit/common/test_bitbucket_client_forge.py`
  → 133 passed. New tests:
  - follows a same-host 302 and returns the resolved JSON body
  - `_paginate` resumes from a redirected resource and collects values
  - rejects a redirect `Location` to a foreign host (SSRF) before re-issuing
  - aborts after `max_redirects` hops with `BITBUCKET_API_ERROR`
  - `fetch_pr_status` populates `changed_paths` from a redirected diffstat
- `ruff check` / `ruff format --check` on the touched files → clean
- `mypy` → Success, no issues in 356 source files

Broad AWF/GitHub validation (full suite + coverage gate) is owned by AWF after the
agent phase.
