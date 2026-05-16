# Plan: Distinguish post-agent commit/pre-commit failures from agent timeout and repair format-only pre-commit failures

Workspace: `ws_aec83fe1696e4bf09d44ab4e`
Branch: `awf/ws_aec83fe1696e4bf09d44ab4e`
Profile: `.awf/workspace.yml` (`awf-self`)

## Problem

Two post-agent commit defects in `awf.control.executor.WorkspaceExecutor`:

1. **Loss of provider/timeout reason codes when the post-agent commit step
   raises.** When the agent CLI raises `AgentRunError(reason_code=AGENT_IDLE_TIMEOUT)`
   (or `AGENT_TIMEOUT`, or any provider-specific code), the executor captures
   `agent_run_reason_code = exc.reason_code` and keeps salvaging the worktree
   (`src/awf/control/executor.py:1569-1581`). If the subsequent `git add` /
   `git commit` step throws — including the common case of a pre-commit
   hook exiting non-zero — control jumps to the generic
   `except Exception` block at `executor.py:2046-2079` and calls
   `_mark_failed(..., failure_reason=infrastructure_failure, reason_code=None)`.
   `_mark_failed` falls back to `failure_reason.value.upper()` so the workspace
   ends up as `INFRASTRUCTURE_FAILURE` with no trace of the original
   `AGENT_IDLE_TIMEOUT`. The "no commits to push" branch (line 1924-1934)
   already preserves `agent_run_reason_code`; the commit-failure branch does
   not.

2. **No distinct classification or repair for pre-commit hook failures during
   post-agent commit.** When `git commit` is rejected because the project's
   pre-commit pipeline (`.pre-commit-config.yaml`, e.g.
   `awf-ruff-format-check` running `uv run --python 3.12 --extra dev ruff
   format --check .`) exits non-zero, the executor raises
   `RuntimeError(f"post-agent commit failed (exit={...}): {stderr}")` at
   `executor.py:1902-1906` and surfaces it as a generic infrastructure
   failure. Operators (and downstream provider recovery / PR monitor)
   cannot tell this apart from "executor crashed" failures; the format-only
   case is also deterministically repairable but AWF makes no attempt.

The PR #236 terminal runtime cleanup (`src/awf/control/worker.py`,
`src/awf/service/orphan_resources.py`) is orthogonal: it runs **after** the
workspace is already terminal and only releases live runtime while preserving
salvage volumes/worktrees. This plan does not touch that path.

## Goals

1. Add a narrow classifier for the post-agent `git add` / `git commit` step
   that produces structured reason codes distinct from
   `INFRASTRUCTURE_FAILURE` and from `AGENT_IDLE_TIMEOUT` / `AGENT_TIMEOUT`:
   - `POST_AGENT_GIT_ADD_FAILED` — `git add -A` exited non-zero (raised
     today at `executor.py:1825-1829`).
   - `POST_AGENT_COMMIT_PRECOMMIT_FAILED` — `git commit` exited non-zero
     and the captured output matches a pre-commit hook failure shape
     (e.g. `pre-commit` framing, hook id `awf-ruff-check`, `awf-mypy`,
     `trailing-whitespace`, `end-of-file-fixer`, etc.).
   - `POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED` — narrower subcase: the
     hook output reports `Would reformat: …` from `ruff format --check`
     and nothing else (no other hook failure).
   - `POST_AGENT_COMMIT_FAILED` — `git commit` exited non-zero for any
     other reason (e.g. detached HEAD, identity missing). Replaces the
     bare `RuntimeError(... "post-agent commit failed" ...)` text.
   These map to `FailureReason.infrastructure_failure` (consistent with
   existing post-agent commit failures), but with a *structured* reason
   code in the failed event and `failure_message`.
2. Preserve `agent_run_reason_code` (and `agent_run_details`) when the
   commit step fails after a non-zero agent exit. The post-agent commit
   wrapper must call `_mark_failed` with the agent's reason code if it is
   set, and put the commit-step diagnostics into the message / payload
   `details` instead of overwriting the reason.
3. On `POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED`, attempt a single
   deterministic repair pass scoped to the agent's diff:
   - Parse the staged file list captured a few lines earlier
     (`cached.stdout` at `executor.py:1830`) for the Python paths.
     Intersect with the `Would reformat: <path>` lines extracted from the
     captured commit stderr/stdout so the formatter only touches files
     the pre-commit hook actually flagged.
   - Run `uv run --python 3.12 --extra dev ruff format <paths>` (i.e. the
     non-`--check` variant of the local hook entry) against just those
     files. Do not run `ruff format .`; do not bypass pre-commit.
   - `git add` those files only (no `-A`), then retry the commit once.
     The full pre-commit pipeline still runs on the retry, so a remaining
     hook failure (e.g. mypy, trailing whitespace in an unrelated file)
     still aborts the workspace — now with reason code
     `POST_AGENT_COMMIT_PRECOMMIT_FAILED` and a clearly attributed
     `format_repair_attempted=true` payload key.
   - The repair never fires for non-format pre-commit failures. If the
     classifier sees any non-format hook failure, the workspace is failed
     immediately with `POST_AGENT_COMMIT_PRECOMMIT_FAILED`.
4. Emit a structured event each time the format-repair path runs
   (`workspace.post_agent_commit_format_repair`) with the repaired paths
   and the retry outcome, so operators / dashboards / the
   conformance/observability tooling can reason about repair frequency
   without parsing free-form messages.
5. Keep the `agent_run_reason_code` / `agent_run_details` chain intact for
   the "no commits salvaged" branch and for the format-repair retry that
   still fails. The agent's original reason wins; the commit/precommit
   classifier appends its evidence under `details["post_agent_commit"]`.

## Non-goals

- **Do not** touch PR #236's terminal runtime cleanup
  (`worker._maybe_release_terminal_runtime`, `orphan_resources._classify`).
  Their tests stay green; the new path adds no `terminal_runtime_*` events.
- **Do not** rewrite the validation-fix-cycle commit path
  (`executor.py:2747-2883`). That block already classifies validation
  failures separately and has its own repair semantics; only the
  pre-validation `git add` / `git commit` block changes.
- **Do not** change scheduler/worker dispatch, provider recovery, profile
  resolution, planning, PR monitor, or runtime-cleanup pipelines.
- **Do not** change coverage policy, ruff config, mypy config, the
  `.pre-commit-config.yaml`, or `pyproject.toml`. The repair path consumes
  the existing hook entry verbatim.
- **Do not** add a dependency on the `pre-commit` Python API. The
  classifier reads only the captured stderr/stdout of `git commit`.
- **Do not** silently bypass any pre-commit hook. The repair retries
  through the full pipeline.
- **Do not** introduce a generic "retry the commit on any failure" loop.
  Only the deterministic format-only case retries.

## Files to touch

### Source

- `src/awf/control/executor.py`
  - New helper module-scope function or `@staticmethod`:
    `_classify_post_agent_commit_failure(result: CommandResult) -> _PostAgentCommitClassification`.
    Returns a small dataclass with: `reason_code` (one of the four codes
    above), `format_repair_files: tuple[str, ...]` (only populated for
    the format-only case), `failed_hooks: tuple[str, ...]` (parsed from
    `pre-commit` framing), `summary: str` (truncated, redacted human
    blurb for `failure_message`).
  - New helper coroutine
    `_run_post_agent_format_repair(*, workspace_id, worktree_path,
    repair_files, profile, agent_run_reason_code, agent_run_details,
    base_commit, ws) -> _PostAgentCommitRepairOutcome`. Runs the scoped
    `ruff format`, restages, retries the commit, and reclassifies on
    second failure. Lives next to the existing commit block so it can
    reuse `_git_in_worktree`, `_repair_agent_git_ownership`, and
    `_runner.run` already in scope.
  - Replace the bare `RuntimeError("post-agent git add failed …")` and
    `RuntimeError("post-agent commit failed …")` raises with a
    `_PostAgentCommitStepError` carrying the captured `CommandResult` and
    its classification. The outer `except Exception` block at
    `executor.py:2046-2079` learns to handle that type explicitly: it
    calls a new `_mark_post_agent_commit_failed(...)` helper that
    receives `agent_run_reason_code` / `agent_run_details` and chooses
    between preserving the agent reason (when set) and using the
    structured commit reason code (when no agent reason exists).
  - The format-repair branch lives inside the same `try` so a successful
    retry continues into the regular `rev-list --count` / `is-ancestor`
    sequence. A still-failing retry raises `_PostAgentCommitStepError`
    with the second classification.
  - Add module-scope constants:
    `POST_AGENT_GIT_ADD_FAILED_REASON_CODE = "POST_AGENT_GIT_ADD_FAILED"`,
    `POST_AGENT_COMMIT_FAILED_REASON_CODE = "POST_AGENT_COMMIT_FAILED"`,
    `POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE = "POST_AGENT_COMMIT_PRECOMMIT_FAILED"`,
    `POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE = "POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED"`.
    Co-locate with the existing `GIT_OBJECT_MISSING_*` /
    `POST_VALIDATION_CONFORMANCE_*` constants near
    `executor.py:185-201`.
  - The event emission for the repair attempt uses
    `WorkspaceRepository.add_event(event_type="workspace.post_agent_commit_format_repair", reason_code=POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE, payload=...)`.
    The terminal failure event is the existing
    `workspace.state_changed → failed` event with the new reason code in
    `payload["reason_code"]` (already supported by `_mark_failed`).

- `docs/REASON_CATALOG.md`
  - Add four catalog entries (one per new reason code) under their
    alphabetical slots. Each entry follows the existing template
    (Problem / Likely Cause / Operator Fix / Related Command /
    Docs Link). The operator fix for the format-rewrite case points at
    re-running `uv run --python 3.12 --extra dev ruff format .` locally
    if AWF's automatic repair was off; the pre-commit case points at
    `uv run --python 3.12 --extra dev pre-commit run --all-files`.

### Tests (TDD — written before any executor edits)

All new tests live under `tests/unit/control/`. They use the existing
`FakeCommandRunner` queue model and `_seed_ready` fixtures so they stay
identical in style to `test_executor_error_paths.py::TestCommitStepRuntimeError`.

- `tests/unit/control/test_executor_post_agent_commit.py` (new file —
  keeps the new branch coverage self-contained, easier to read in PR
  review than appending to the 8k-line existing files):
  1. `test_post_agent_commit_precommit_failure_uses_precommit_reason_code`
     — agent exits 0; `git commit` exits 1 with stderr matching
     `pre-commit` framing for the `awf-mypy` hook. Expect the workspace
     `failed` with `failure_reason=infrastructure_failure` AND the new
     reason code `POST_AGENT_COMMIT_PRECOMMIT_FAILED` in the latest
     `workspace.state_changed` event payload. No retry happens (only one
     `git commit` is queued/consumed).
  2. `test_post_agent_commit_format_only_failure_repairs_and_retries`
     — agent exits 0; `git commit` exits 1 with stdout `Would reformat:
     src/foo.py\n1 file would be reformatted\n` and pre-commit framing
     for the `awf-ruff-format-check` hook id only. Cached diff lists
     `src/foo.py`. Expect: a `ruff format src/foo.py` invocation (we
     match argv tail), a second `git add -- src/foo.py`, a second
     `git commit` (exits 0), then validation runs and the workspace
     reaches `completed`. A `workspace.post_agent_commit_format_repair`
     event is present with `payload["repaired_paths"] == ["src/foo.py"]`
     and `payload["retry_outcome"] == "succeeded"`.
  3. `test_post_agent_commit_format_repair_retry_still_fails_marks_precommit`
     — same as (2) but the second `git commit` exits 1 with a non-format
     pre-commit failure (mypy). Expect:
     `POST_AGENT_COMMIT_PRECOMMIT_FAILED` reason code, message includes
     "format repair attempted", and the repair event's
     `payload["retry_outcome"] == "failed"`.
  4. `test_post_agent_commit_format_only_skips_files_outside_diff`
     — pre-commit reports `Would reformat: legacy/untouched.py` but the
     staged diff only contains `src/foo.py`. Expect: NO `ruff format`
     invocation (no candidates after intersection), workspace fails with
     `POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED` (the classifier saw a
     format-only failure but no agent-owned files to repair). The repair
     event records `payload["repaired_paths"] == []` and
     `payload["retry_outcome"] == "skipped"`.
  5. `test_post_agent_commit_failure_preserves_agent_idle_timeout_reason`
     — agent raises `AgentRunError(reason_code="AGENT_IDLE_TIMEOUT",
     details={"provider":"anthropic", ...})`; salvage path runs;
     `git commit` then exits 1 (pre-commit format failure). Expect:
     workspace failed with `reason_code="AGENT_IDLE_TIMEOUT"` (NOT
     `POST_AGENT_COMMIT_PRECOMMIT_FAILED`), `failure_message` mentions
     both the timeout salvage note AND the commit-step evidence, and the
     `payload["details"]["post_agent_commit"]["reason_code"]` carries
     the commit classification for observability. `failure_reason` stays
     `infrastructure_failure` (the agent timeout was already classified
     `agent_failure` upstream — verify which by reading current behavior
     and matching it; the existing
     `test_agent_failure_with_no_work_marks_failed` shows the failure
     mapping for the "no commits" branch, which is what this test
     mirrors with a commit-failure twist).
  6. `test_post_agent_git_add_failure_uses_git_add_reason_code`
     — `git add -A` exits 128. Expect:
     `POST_AGENT_GIT_ADD_FAILED` reason code, no commit attempted, no
     repair attempted.
  7. `test_post_agent_commit_non_precommit_failure_uses_generic_reason`
     — `git commit` exits 1 with stderr `fatal: empty ident name (for
     <>) not allowed` (no pre-commit framing). Expect:
     `POST_AGENT_COMMIT_FAILED` reason code (not the
     `INFRASTRUCTURE_FAILURE` default).

- `tests/unit/control/test_executor_error_paths.py`
  - Update existing
    `TestCommitStepRuntimeError::test_nonzero_git_commit_raises_and_marks_failed`
    to assert the new structured reason code
    (`POST_AGENT_COMMIT_FAILED`) instead of accepting any
    `infrastructure_failure`. The test stays in-place; only its
    assertions tighten.

- `tests/unit/control/test_executor_post_agent_commit_classifier.py`
  (new tiny file — unit tests for the classifier helper without the
  executor):
  1. Pre-commit failure with hook id `awf-ruff-check` → `POST_AGENT_COMMIT_PRECOMMIT_FAILED`.
  2. Pre-commit failure with only `awf-ruff-format-check` AND
     `Would reformat: …` lines → `POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED`
     plus parsed paths.
  3. Generic `fatal: …` git output → `POST_AGENT_COMMIT_FAILED`.
  4. Empty stderr with non-zero exit → `POST_AGENT_COMMIT_FAILED` (no
     spurious format match).
  5. Hook output that contains `Would reformat:` AND another failing
     hook (`awf-mypy`) → `POST_AGENT_COMMIT_PRECOMMIT_FAILED` (no
     repair).
  6. Hook output with `Would reformat:` paths that include directories
     outside the staged set is preserved verbatim; intersection happens
     in the repair coroutine, not the classifier.

- `tests/unit/test_pre_commit_hooks.py`
  - Append a regression assertion that the
    `.pre-commit-config.yaml` `awf-ruff-format-check` hook entry is
    still the deterministic `ruff format --check .` form the executor
    expects to parse. This locks the contract surface so a future
    pre-commit refactor that breaks the classifier triggers a fast,
    local unit test failure.

### Documentation

- `docs/REASON_CATALOG.md` — four new entries (above).
- No new top-level docs are added. The plan
  (`docs/awf-plans/ws_aec83fe1696e4bf09d44ab4e.md`) and the catalog
  cover the new reason codes; the existing executor docstring at the
  top of `src/awf/control/executor.py` already documents the failure
  taxonomy with `FailureReason`, so we only extend the in-file
  comments adjacent to the new helper.

## Implementation order

1. **Failing tests first.** Write `test_executor_post_agent_commit_classifier.py`
   (smallest scope — the classifier returns dataclasses, no I/O). Run
   `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_post_agent_commit_classifier.py -q`
   and confirm failures with module-not-found / function-not-found.
2. Add the classifier helper + reason-code constants in
   `src/awf/control/executor.py`. Re-run (1); confirm green.
3. Write `tests/unit/control/test_executor_post_agent_commit.py`
   (executor-level scenarios). Run targeted pytest; confirm each test
   fails for the expected reason (e.g. current behavior overwrites
   reason code, or no repair invocation observed).
4. Update the post-agent commit block:
   - Replace the two bare `RuntimeError` raises with
     `_PostAgentCommitStepError`.
   - Add the format-repair coroutine and call it inline.
   - Update the outer `except Exception` to route
     `_PostAgentCommitStepError` to the new
     `_mark_post_agent_commit_failed` helper which honors
     `agent_run_reason_code` / `agent_run_details`.
   - Re-run all targeted tests; confirm green.
5. Update the existing
   `TestCommitStepRuntimeError::test_nonzero_git_commit_raises_and_marks_failed`
   assertion to the new reason code; confirm green.
6. Add the `docs/REASON_CATALOG.md` entries and the
   `tests/unit/test_pre_commit_hooks.py` assertion.
7. Run the broader executor and pre-commit hook suites locally:
   `tests/unit/control/test_executor.py`,
   `tests/unit/control/test_executor_error_paths.py`,
   `tests/unit/control/test_executor_coverage_edges.py`,
   `tests/unit/test_pre_commit_hooks.py`. Investigate any regressions
   (especially in tests that asserted on the old `infrastructure_failure`
   reason code) and update assertions only where the new reason code is
   strictly more specific.
8. Run the focused validation gates:
   `uv run --python 3.12 --extra dev ruff check src/awf tests`,
   `uv run --python 3.12 --extra dev mypy src/awf`,
   `uv run --python 3.12 --extra dev pytest tests/unit/control tests/unit/test_pre_commit_hooks.py -q`.

## Validation commands

The narrowest commands that prove the change, then the wider gates the
profile (`.awf/workspace.yml`) and `AGENTS.md` require:

```bash
# 1) New classifier unit test
uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_executor_post_agent_commit_classifier.py -q

# 2) New executor-level scenarios + existing commit-step regression
uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_executor_post_agent_commit.py \
  tests/unit/control/test_executor_error_paths.py::TestCommitStepRuntimeError \
  tests/unit/test_pre_commit_hooks.py -q

# 3) Full executor suite — guards against regressions in salvage,
#    branch-drift recovery, validation fix cycle, PR creation.
uv run --python 3.12 --extra dev pytest tests/unit/control -q

# 4) Style + types, scoped to touched modules
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf

# 5) Profile-aligned validate phases (matches .awf/workspace.yml)
uv run --python 3.12 --extra dev ruff check src/awf/cli tests/unit/cli
uv run --python 3.12 --extra dev mypy src/awf/cli
uv run --python 3.12 --extra dev pytest tests/unit/cli -q
uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_test_quality_guardrails_self.py -q
```

The conformance report (`docs/awf-plans/ws_aec83fe1696e4bf09d44ab4e.conformance.json`)
will reference the (2) and (3) runs.

## Risks and mitigations

- **Risk: classifier false-positives — `ruff format --check` happens to
  print `Would reformat:` inside an output that ALSO carries another
  failed hook.**
  Mitigation: the classifier only flags `POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED`
  when the parsed failed-hook set is `{"awf-ruff-format-check"}` (or
  equivalent — both `pre-commit`'s hook id and the framework's
  "X failed" / "X passed" framing must agree). Anything else falls
  through to `POST_AGENT_COMMIT_PRECOMMIT_FAILED`. Tests (1) and (5) in
  the classifier file lock this.
- **Risk: classifier false-negatives — pre-commit version drift changes
  the framing.**
  Mitigation: the new
  `tests/unit/test_pre_commit_hooks.py` assertion keeps the hook entry
  shape pinned; if a future PR re-organises the entries, the test fails
  fast and the classifier patterns are revisited in the same PR.
- **Risk: format-repair mutates more files than the agent's diff.**
  Mitigation: we only pass files from the intersection of the staged
  set and the `Would reformat:` list to `ruff format`. Test (4) in
  `test_executor_post_agent_commit.py` exercises the empty-intersection
  case and asserts no `ruff format` invocation.
- **Risk: format-repair masks a real pre-commit failure (we silently
  reformat then succeed, but the agent's intent was to e.g. test that
  a generator emits non-formatted output).**
  Mitigation: the repair only runs for files the agent actually staged
  and only when the SOLE failing hook is the format check. The retry
  still runs the full pipeline. Any non-format hook still aborts.
- **Risk: salvaging a timeout but then losing the reason code under
  test environments where post-agent commit normally succeeds, masking
  the bug for future maintainers.**
  Mitigation: test (5) in `test_executor_post_agent_commit.py`
  explicitly drives the AGENT_IDLE_TIMEOUT + commit-fail combination and
  asserts the agent reason wins.
- **Risk: PR #236 terminal runtime cleanup tests notice new event
  types and need adjustment.**
  Mitigation: the new `workspace.post_agent_commit_format_repair` event
  is emitted during the `running` phase, never against terminal
  workspaces, so the runtime-release sweep's idempotency check
  (`workspace.terminal_runtime_released` NOT-EXISTS) is unaffected.
  Verify by running the relevant slice of `tests/unit/control/test_worker.py`
  (the suite that PR #236 added/changed) as part of the broad executor
  test pass; it should remain green untouched.
- **Risk: coverage policy regression — adding code without
  proportional tests.**
  Mitigation: the new tests target every new branch, including the
  intersection-empty path and the retry-still-fails path. Running the
  full unit suite once locally before pushing confirms the 99% target
  remains.

## Assumptions

- The post-agent commit step at `executor.py:1819-1906` is the only
  place pre-commit hooks run during the AWF "salvage agent work" phase.
  The validation-fix-cycle commit block (line 2861-2872) does NOT run
  inside the same `try` and uses a separate failure surface
  (`fix_pass_commit_failed`), so this plan does not need to touch it.
  (Verified by reading the surrounding control flow.)
- `git commit` captures pre-commit hook stderr/stdout on the same
  `CommandResult` AWF inspects today; no extra stream plumbing is
  needed. `FakeCommandRunner` already supports `stderr=...` and
  `stdout=...` for the queued result.
- `_mark_failed` already accepts `details=Mapping[str, Any]` for
  payload context; no schema migration is required to add
  `details["post_agent_commit"]`.
- The profile's `setup` phase has already run `pre-commit install
  --install-hooks` (`.awf/workspace.yml:46-48`), so a real workspace
  reaches the post-agent commit step with hooks active. Test doubles
  do not need to install pre-commit — they only need to return canned
  stderr/stdout shaped like a pre-commit failure.
- `ruff format <paths>` (without `--check`) exits 0 even when it
  rewrites files, matching `ruff format`'s documented behavior. If a
  future ruff release flips this, the retry's exit-code branch still
  runs the second commit (which re-runs `--check`) and the workspace
  fails deterministically.
- Reason codes are surfaced through existing channels (failed event
  payload, `Workspace.failure_message`, CLI/API/MCP serialisers that
  read those columns). No new surface plumbing is needed.

## Out-of-scope follow-ups (not in this PR)

- Auto-repair for non-format pre-commit hooks (e.g. trailing whitespace
  fixer). Possible but each hook has its own semantics; out of this
  slice.
- A repair turn to the agent itself when format-repair is impossible.
  Mentioned in the task as an acceptable alternative; we choose the
  deterministic local fix instead because it is strictly cheaper and
  the task says "Prefer the minimal deterministic path already aligned
  with repo conventions."
- Surfacing the new reason codes in the console dashboard. The codes
  flow through the existing event payload; the console layer reads
  generic `reason_code` strings and need no change to display them.
