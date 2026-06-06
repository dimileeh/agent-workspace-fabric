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

## Live-docs verification (plan gate) — COMPLETED (iteration 1)

The plan required verifying every endpoint/verb/field against live Atlassian Cloud REST
v2.0 docs. The HTML docs at `developer.atlassian.com/cloud/bitbucket/rest/` are
JS-rendered (truncated shells over `WebFetch`/headless), but the **authoritative,
machine-readable OpenAPI 2.0 spec** is published at
`https://api.bitbucket.org/swagger.json` (950 KB, `basePath: /2.0`,
`host: api.bitbucket.org`). Iteration 1 fetched it and verified every method against it
(and against the embedded request-body examples in the endpoint descriptions, which are
the live docs' canonical curl snippets).

**Verified correct (9/10 mappings, all endpoints/verbs/fields):**

| Method | Endpoint / verb | Spec confirmation |
|---|---|---|
| create_pull_request | `POST .../pullrequests` | path+verb present |
| fetch_pr_status | `GET .../pullrequests/{id}`, `GET .../commit/{sha}/statuses`, `GET .../comments`, `GET .../diffstat` | all present |
| fetch_failing_check_logs | `GET .../commit/{sha}/statuses` → `GET .../pipelines` → `GET .../pipelines/{uuid}/steps` → `GET .../pipelines/{uuid}/steps/{uuid}/log` | all present; **singular `/log`** confirmed (spec also exposes `/logs/{log_uuid}` — plan correctly chose `/log`); step result `state.result.name == "FAILED"` matches `pipeline_step_state_completed_failed` |
| resolve_thread | `POST .../comments/{id}/resolve` | spec: **POST = "Resolve a comment thread", DELETE = "Reopen"** — impl correctly uses POST |
| post_comment | `POST .../comments` `{content:{raw}}` | present |
| create_issue | `POST .../issues` | present |
| fetch_repo_merge_methods / fetch_branch_* | from PR `destination.branch.merge_strategies` | merge-strategy values match the `merge_strategy` enum below |
| merge_pr | `POST .../pullrequests/{id}/merge` `{merge_strategy, close_source_branch}` | `merge_strategy` enum = `merge_commit, squash, fast_forward, squash_fast_forward, rebase_fast_forward, rebase_merge` → D5 map (`merge→merge_commit`, `squash→squash`, `fast_forward→fast_forward`) all valid |

Supporting enums confirmed: commit-status `state` = `FAILED, INPROGRESS, STOPPED, SUCCESSFUL`
(impl handles all); PR `state` = `OPEN, DRAFT, QUEUED, MERGED, DECLINED, SUPERSEDED`
(impl treats MERGED→merged, DECLINED/SUPERSEDED→closed, and OPEN/DRAFT/QUEUED→non-terminal,
which is correct — the live enum adds DRAFT/QUEUED beyond the plan's list, both safely
non-terminal).

**Bug found and fixed by this gate (the reason the gate exists):** the
`rerun_failed_workflow_jobs` pipeline-trigger body built the selector as
`{"type": "pull_requests", ...}` (underscore). The live "Trigger a pull request pipeline"
example — confirmed in both the JSON body and the on-demand query-param form
(`target.selector.type=pull-requests`, YAML key `pull-requests:`) — uses the **hyphenated
`"pull-requests"`**. An underscore would fail to match the `pull-requests` pipeline
definition and break the rerun. Fixed in `bitbucket_client.py`; the
`test_rerun_reconstructs_pr_pipeline_target` test now asserts
`target["selector"] == {"type": "pull-requests", "pattern": "**"}` (test-first; it failed
against the underscore, passes after the fix). The `pipeline_pullrequest_target` type and
the `"pullrequest": {"id": ...}` key (no underscore) were verified correct.

**Documented nuances (verified, intentionally unchanged):**
- The `pullrequest_merge_parameters` schema marks `type` as `required`, but it is the
  generic base-object discriminator with no documented value and the live merge endpoint
  accepts `{merge_strategy, close_source_branch}` without it (as ubiquitous real-world BB
  merge integrations do). Adding an unknown `type` value would risk breaking merge, so it
  is intentionally omitted.
- The PR pipeline-trigger doc example shows `"id": "3"` (string); the impl sends the
  numeric PR id, which BitBucket accepts (ids are numeric everywhere else in v2.0).
- `…/pipelines` and `…/pipelines/{uuid}/steps` are listed in the spec without a trailing
  slash, but the live Pipelines collection requires the trailing-slash form
  (`…/pipelines/`); the impl and test fakes use the trailing-slash form per the documented
  curl examples. Pipeline/step UUIDs are URL-encoded.

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
