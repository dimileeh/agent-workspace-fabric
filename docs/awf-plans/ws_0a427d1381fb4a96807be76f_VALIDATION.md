# Validation — BitBucket monitor deferrals #448 / #444 / #445 (one PR)

Workspace: `ws_0a427d1381fb4a96807be76f`
Plan: `docs/awf-plans/ws_0a427d1381fb4a96807be76f.md`

Strict TDD throughout: failing test(s) first, then the smallest green change. Focused
validation only (AWF + GitHub CI own the full coverage/OpenAPI/console gates).

## #448 — fast_forward merge gate ✅

- `merge_loop.py:_MERGE_METHOD_PREFERENCE` now `("squash","merge","rebase","fast_forward")`
  — `fast_forward` **last**, so squash stays default and GitHub precedence is unchanged.
  `_KNOWN_MERGE_METHODS` derives from it, so the policy intersection no longer drops a
  fast-forward-only repo to an empty tuple.
- Tests (`tests/unit/runtime/test_pr_monitor_merge_methods.py`): ff-only repo →
  `("fast_forward",)`; multi-strategy → squash first; GitHub order unchanged; full-loop
  ff-only BB repo merges via `fast_forward` with no MERGE_METHOD_MISMATCH blocker. BB
  `merge_pr(method="fast_forward")` → strategy `fast_forward` already covered by the
  existing `test_merge_pr_strategy_round_trip`.

## #444 — shared `ForgeClientError` base ✅

- New neutral module `src/awf/common/forge_errors.py` holds `ForgeClientError`. Both
  `GitHubClientError` and `BitBucketClientError` extend it. Normalized accessors on the
  base (concrete defaults, overridden per subclass): `reason_code` (GitHub defaults to a
  new stable `GITHUB_API_ERROR`; BitBucket keeps its native code), `redacted_detail()`
  (GitHub stderr / BitBucket body), `merge_method_stderr()` (GitHub stderr / `""`
  elsewhere), `http_status` (GitHub `None` / BitBucket `status`). No subclass field was
  renamed — existing `exc.stderr/.body/.status/.returncode` reads are unchanged.
- New `_wait_after_transient_forge_error` (transient_ops) dispatches by subclass to the
  existing `_wait_after_transient_github_error` / `_wait_after_transient_bitbucket_error`,
  so each forge keeps emitting its own transient-retry event + reason code
  (`GITHUB_TRANSIENT_RETRY` / `BITBUCKET_TRANSIENT_RETRY`) — behaviour-preserving by
  construction.
- Catch sites consolidated from dual `except GitHubClientError / except
  BitBucketClientError` arms to a single `except ForgeClientError`: `fix_cycle` settle
  re-poll, resolve, deferred-capture create-issue + courtesy-comment; `runner` status
  fetch + execute-action; `loop` workflow-scope notify, CI rerun, human notify;
  `merge_loop` pre-merge recheck capture + post-human-notify. Divergent fields
  (terminate reason_code, monitor_log `reason`, operation outcome strings, transient
  audit reason code) are preserved per-forge via `isinstance` selection / normalized
  accessors.
- **GH merge-method path left provably unchanged (risk #1):** the GH
  `_attempt_merge_method` arm stays `except GitHubClientError` (it parses `exc.stderr`
  for the squash→merge→rebase retry + MERGE_METHOD_MISMATCH recording); the BB merge arm
  stays a separate explicit `except BitBucketClientError`; the `isinstance(merge_blocker,
  BitBucketClientError)` dispatch and the GH merge-method preflight arm are untouched.
- Untouched per plan: `release_pr_sync.py` (GH-only duplicate-PR returncode/stderr) and
  `control/executor/monitor_handoff*.py` (executor-owned, out of scope).
- Tests: `tests/unit/common/test_forge_errors.py` (isinstance + every normalized
  accessor for both subclasses + base defaults). All existing GH/BB classification and
  merge-method regression tests pass unchanged through the base.

## #445 — BitBucket PR-tasks gating ✅

- `fetch_pr_status` now paginates `GET .../pullrequests/{id}/tasks` and maps UNRESOLVED
  tasks into feedback so a PR with open tasks but no comments no longer reaches `Merge`.
- Auto-resolve mirrors comment-thread resolution: a task-discriminated neutral id
  (`bbtask:<owner>/<name>#<pr>:<task>`, codec in `bitbucket_client_parsing`) routes
  `resolve_thread` to `PUT .../tasks/{id}` `{"state":"RESOLVED"}`. A 403 re-raises as the
  new stable `BITBUCKET_TASK_RESOLVE_FORBIDDEN`.
- Safety: the fix-cycle calls resolve only for resolvable verdicts (no closing a task
  without evidence; `agent_failed`/`needs_human` already excluded). A failed PUT raises,
  so the task stays UNRESOLVED and keeps blocking. A **permanent** task-resolve failure
  is downgraded to `needs_human` (kept addressed) rather than cleared — so it does NOT
  re-route to the agent (no retry storm) and `decide()` escalates to NotifyHuman.
- Forge-aware provenance: `feedback_state.py` derives `scm_provider` + PR URL from the
  resolved forge client (`_forge_scm_provider` / `_forge_pr_url`) instead of hardcoded
  `github` + a github.com URL — BitBucket feedback no longer poisons GitHub
  provenance/replay rows.
- New reason code `BITBUCKET_TASK_RESOLVE_FORBIDDEN` defined in the client (a client
  file); reason-catalog coverage gate passes.
- Tests: `test_bitbucket_client_part_006.py` (task-id codec; UNRESOLVED→thread mapping;
  resolved/viewer/empty/id-less dropped; open-task-no-comments blocks merge via
  `decide()`; tasks paginate; PUT resolve body+url; 403→stable code; non-403 propagates).
  `test_pr_monitor_runner_part_003.py` (BB provenance recorded under `bitbucket` +
  bitbucket.org URL, GitHub provenance unchanged). `test_pr_monitor_runner_coverage_
  edges_part_002.py` (forbidden task-resolve → `needs_human`, no storm, not terminated).

### Deviation from the locked plan (documented)

The plan's locked `#445` decision was to surface tasks as `ReviewComment` with
`source_kind="task"` into `unresolved_review_comments`, asserting "the address-comments
gate already treats [it] as merge-blocking pending work." **Verification (which the plan
explicitly required — risk: "confirm no bot/issue carve-outs") showed this is not true:**
`decide()` only routes review comments to `AddressComments` when
`_agent_can_triage_review_comment` (i.e. `source_kind=="issue"` AND a bot author), and a
review comment with no recorded verdict never blocks merge (gate 8 checks recorded
verdicts only). A reviewer-authored `source_kind="task"` comment would therefore neither
route to the agent nor block merge — the opposite of the requirement.

The minimal correct alternative (chosen) maps tasks to the **inline-thread feed**
(`unresolved_inline_threads`), which already (a) routes every item to `AddressComments`
regardless of author, (b) blocks merge until resolved, and (c) resolves through
`resolve_thread` — the exact mechanism the plan's auto-resolve design says to mirror.
This requires **no change to `decide()`'s core gates** (lower GitHub-regression risk than
patching the comment gate) while satisfying every #445 acceptance test. GitHub never emits
tasks, so GitHub assembly + provenance are unchanged.

## Focused validation evidence

- `ruff check .` ✅ · `ruff format --check .` ✅ · `mypy` (no path args) ✅
- `pytest tests/unit/common/test_forge_errors.py` ✅ (6)
- `pytest tests/unit/common/test_bitbucket_client_parts` ✅ (includes part_006 tasks)
- `pytest tests/unit/runtime/test_pr_monitor_merge_methods.py` ✅
- `pytest tests/unit/runtime/test_pr_monitor_runner_parts` ✅
- `pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts` ✅
- `pytest tests/unit/docs/test_catalog_coverage.py` ✅
- Broad regression sweep `pytest tests/unit/runtime tests/integration/runtime/...
  tests/unit/control/test_executor_error_paths_parts` → 2012 passed.
- Focused coverage: `forge_errors.py` 100%; new task parsing/codec lines fully covered;
  new `bitbucket_client` task lines covered.

The full `pytest --cov` 99% gate, OpenAPI drift, console, and broad validation are owned
by AWF + GitHub CI after the agent phase.
