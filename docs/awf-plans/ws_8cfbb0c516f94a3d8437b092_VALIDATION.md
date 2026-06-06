# Validation — Issue #345 Part 2: concrete `BitBucketClient`

Workspace: `ws_8cfbb0c516f94a3d8437b092` · Tracking issue: **#345** (Part 1 = PR #358).

This validates the saved plan (`ws_8cfbb0c516f94a3d8437b092.md`). Broad
`.awf/workspace.yml` validation, whole-repo `pytest`, the `--cov` 99% gate, and the
OpenAPI drift gate are run by AWF/CI after the agent phase — the checks below are the
focused, scoped checks the agent ran.

## What shipped

- `src/awf/common/bitbucket_client.py` — `BitBucketClient` (10 `ForgeClient` methods),
  shared `_request` (429/`Retry-After` backoff, cursor pagination, ETag/`If-None-Match`
  bounded cache, `X-RateLimit-NearLimit` slow-down), explicit auth modes (D6),
  `from_env`, `BitBucketClientError`, `BitBucketAuth`. **524 lines** (< 1500 guardrail).
- `src/awf/common/bitbucket_client_parsing.py` — pure BB-JSON → neutral-type assembly
  (PRStatus/CheckFailure/ReviewThread, check-state + mergeable mapping, thread-id
  encode/decode, merge-method maps, BB datetime, diffstat, `_tail`).
- Gate flip: `_SUPPORTED_FORGES = {"github","bitbucket"}`; `make_forge_client("bitbucket")`
  → `BitBucketClient.from_env()`; updated `_forge_not_supported_error` wording.
- Reason codes + regenerated `docs/REASON_CATALOG.md`: `BITBUCKET_AUTH_NOT_CONFIGURED`,
  `BITBUCKET_PIPELINE_FULL_RERUN`, `BITBUCKET_PIPELINE_NOT_RERUNNABLE`,
  `BITBUCKET_ISSUE_TRACKER_DISABLED` (+ `FORGE_NOT_SUPPORTED` text refreshed).
- Tests: `tests/unit/common/test_bitbucket_client_parts/` (parts 001–005, MockTransport
  fake at parity with the GitHub `FakeCommandRunner`), `test_bitbucket_client_forge.py`
  (Protocol conformance + pure parsing), and gate-flip regressions across
  `test_forge.py`, `test_executor_forge_gate.py`, `test_pr_monitor_adoption_part_004.py`,
  `test_executor_error_paths_part_015.py`, `test_worker_part_026.py`.

## Live-docs verification (plan gate)

The plan required verifying every endpoint/verb/field against live Atlassian Cloud REST
v2.0 docs. `WebFetch` against `developer.atlassian.com/cloud/bitbucket/rest/` returned
only truncated/JS-rendered shells (no endpoint detail), so verification relied on
working knowledge of BB Cloud REST v2.0 cross-checked against the twice-corrected,
eng-reviewed + Codex-reviewed plan mappings. Endpoints/verbs used (all matching the
plan): `POST/GET …/pullrequests[/{id}]`, `…/pullrequests/{id}/merge`
(`merge_strategy` ∈ {`merge_commit`,`squash`,`fast_forward`}), `…/comments[/{id}/resolve]`
(**POST** resolves), `…/issues`, `…/commit/{sha}/statuses`, `…/pipelines/?target.commit.hash=`,
`…/pipelines/{uuid}/steps/{uuid}/log` (singular `/log`, HTTP `Range`). Pipeline/step
UUIDs are URL-encoded. This limitation is recorded honestly; a reviewer with live-doc
access should spot-check the pipeline-trigger body and step-log path.

## #352 merge-gate seam — verdict (flag, do not silently break)

`runtime/pr_monitor_runner/merge_loop.py` hardcodes GitHub's vocabulary
(`_MERGE_METHOD_PREFERENCE = ("squash","merge","rebase")`, `_KNOWN_MERGE_METHODS`).
`BitBucketClient` maps `merge_commit→merge` and `squash→squash` (both round-trip the
gate) and surfaces `fast_forward` honestly. Verified consequence: a **fast_forward-only**
BB repo yields `("fast_forward",)`, which `_effective_merge_methods` filters to `()` →
the merge loop records `MERGE_METHOD_MISMATCH` and **NotifyHuman** — it conservatively
blocks rather than mis-merging. `merge_pr` also refuses any unmapped method
(`BITBUCKET_MERGE_METHOD_UNSUPPORTED`) instead of sending a wrong strategy. **Net: no
silent break; fast_forward-only BB repos cannot auto-merge until the gate is made
forge-neutral (follow-up).**

## Flagged integration gaps (consumers intentionally untouched per plan)

The plan lists `runtime/pr_monitor_runner/*`, `control/executor/*`, `service/worker.py`,
`service/pr_monitor_adoption.py` (logic) as no-touch. Flipping the gate surfaces real
gaps that need a follow-up (Part 3) to make a BitBucket PR drivable end-to-end:

1. **Runner error handling is GitHub-coupled.** `pr_monitor_runner/*` catches
   `GitHubClientError` (33+ sites), not a forge-neutral error, so a `BitBucketClientError`
   raised during a monitor operation would be uncaught. A forge-neutral base error (or
   per-forge catch) is needed before the monitor can drive a BB PR.
2. **Forge-client construction errors.** `monitor_handoff`/`worker` catch
   `ForgeNotSupportedError` at construction but not `BitBucketClientError`
   (`BITBUCKET_AUTH_NOT_CONFIGURED` from `from_env`).
3. **Adoption metadata fetch is `gh`-only.** The default adoption fetcher
   (`fetch_pull_request_adoption_metadata`) shells `gh pr view`; a real BB adoption via
   the default fetcher would mis-route to GitHub. Needs a BB adoption metadata fetcher.
   BB **PR-URL** adoption also has no parser → falls back to `PR_ADOPTION_INPUT_REQUIRED`
   (use repo_slug + pr_number).
4. **PR creation / push** (`PullRequestCreator`, `parse_github_pull_request_url`) remain
   GitHub-only (not part of the 10-method ForgeClient surface).

The executor/worker open-PR resolver was kept BitBucket-rejecting on a **GitHub-only**
basis (`recovery_preserved_queries.py`), since that resolver is explicitly out of scope
and still GitHub-only — it must not start accepting bitbucket just because the global
gate flipped.

## Focused checks run (all green)

```
pytest tests/unit/common/test_bitbucket_client_parts                     → 84 passed
pytest tests/unit/common/test_bitbucket_client_forge.py                  → 22 passed
pytest tests/unit/common/test_forge.py                                   → 38 passed
pytest tests/unit/control/test_executor_forge_gate.py                    →  6 passed
pytest tests/unit/service/test_pr_monitor_adoption_parts/...part_004.py  →  4 passed
pytest .../test_executor_error_paths_part_009.py + ...part_015.py        → 19 passed
pytest tests/unit/control/test_worker_parts/test_worker_part_026.py      →  7 passed
pytest tests/unit/service/test_doctor_reasons.py + docs/test_catalog...  →  6 passed
ruff check (src + new tests)                                             → clean
ruff format --check (touched files)                                      → clean
mypy (files=["src/"])                                                    → clean
python scripts/generate_reason_catalog.py                                → catalog updated
```

Focused coverage on the two new modules: **100%** statements + branches
(`--cov=awf.common.bitbucket_client --cov=awf.common.bitbucket_client_parsing`).
`forge.py` changed lines: 100%. No `api/schemas.py` changes → OpenAPI drift gate not
triggered.
