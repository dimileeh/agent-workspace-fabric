# Agent Timeout Salvage Continuation Validation

Plan reference: `plans/AGENT_TIMEOUT_SALVAGE_CONTINUATION_PLAN.md`

## Requirement Status

- Retry detects `AGENT_IDLE_TIMEOUT` / `AGENT_TIMEOUT` sources: Complete.
  - Added `_agent_timeout_retry_context()` in `src/awf/service/workspaces.py`.
  - Unit coverage: `test_retry_agent_idle_timeout_auto_salvages_implementation_diff`.
- Timed-out sources with implementation diffs are captured through the existing
  temp-index binary patch flow: Complete.
  - Reused `capture_conformance_salvage()` for timeout retries.
  - Live evidence: retry workspace `ws_6f996a0f4db54b93814ff6bc` captured
    `/Users/dlihhats/.awf/service/artifacts/salvage/ws_0661de05250d40d18951b515-b85a6d31fdba.patch`.
  - Live evidence: retry workspace `ws_0062f80399b34037b05c989a` captured
    `/Users/dlihhats/.awf/service/artifacts/salvage/ws_39eeffb62ae345a8a5e5a0df-f208a4fb14e7.patch`.
- Retry task policy records salvage metadata and executor applies it through the
  existing execution path: Complete.
  - Both live retries have `task_policy.conformance_salvage.salvage_kind =
    "agent_timeout"`.
  - Both live retries emitted `workspace.conformance_salvage_applied` with
    `salvage_kind = "agent_timeout"`.
- Retry prompt identifies automatic timeout salvage: Complete.
  - Added `build_agent_timeout_salvage_retry_prompt()`.
  - Live retry prompts start with `## Automatic AWF timeout salvage`.
- Existing conformance retry behavior remains unchanged: Complete.
  - Existing conformance retry tests still pass.
- Plan-only timeout diffs retry normally without salvage: Complete.
  - Unit coverage: `test_retry_agent_idle_timeout_plan_only_diff_retries_without_salvage`.
- Unit coverage proves timeout salvage capture and prompt/payload metadata:
  Complete.
  - Unit coverage: `test_retry_agent_idle_timeout_auto_salvages_implementation_diff`.
- Unit coverage proves ignored parent-directory patterns do not prevent salvage
  for tracked changed files: Complete.
  - Unit coverage:
    `test_capture_stages_tracked_files_under_ignored_parent_directory`.
- Restart AWF and use AWF retries for failed dogfood workspaces: Complete.
  - Rebuilt/recreated local API and worker containers through
    `awf service bootstrap`; the wrapper was stopped after API/worker were
    recreated because full service readiness remains blocked by pre-existing
    orphan-resource health checks.
  - Verified `/healthz` returned OK.
  - Verified running container code exposes
    `build_agent_timeout_salvage_retry_prompt`.
  - Created AWF retries:
    - `ws_6f996a0f4db54b93814ff6bc` from `ws_0661de05250d40d18951b515`.
    - `ws_0062f80399b34037b05c989a` from `ws_39eeffb62ae345a8a5e5a0df`.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/service/test_workspace_retry.py::test_retry_agent_idle_timeout_auto_salvages_implementation_diff \
  tests/unit/service/test_workspace_retry.py::test_retry_agent_idle_timeout_plan_only_diff_retries_without_salvage \
  tests/unit/service/test_conformance_salvage.py::test_capture_stages_tracked_files_under_ignored_parent_directory \
  tests/unit/service/test_conformance_salvage.py::test_prompt_helpers_handle_long_or_missing_path_lists -q
# 4 passed

uv run --python 3.12 --extra dev pytest \
  tests/unit/service/test_workspace_retry.py \
  tests/unit/service/test_conformance_salvage.py -q
# 31 passed

uv run --python 3.12 --extra dev ruff check \
  src/awf/service/conformance_salvage.py \
  src/awf/service/workspaces.py \
  tests/unit/service/test_workspace_retry.py \
  tests/unit/service/test_conformance_salvage.py
# All checks passed

uv run --python 3.12 --extra dev ruff format --check \
  src/awf/service/conformance_salvage.py \
  src/awf/service/workspaces.py \
  tests/unit/service/test_workspace_retry.py \
  tests/unit/service/test_conformance_salvage.py
# 4 files already formatted

uv run --python 3.12 --extra dev mypy \
  src/awf/service/conformance_salvage.py \
  src/awf/service/workspaces.py
# Success: no issues found in 2 source files
```

## Live AWF Evidence

- `ws_6f996a0f4db54b93814ff6bc`
  - status: `running`
  - source workspace: `ws_0661de05250d40d18951b515`
  - salvage event: `workspace.conformance_salvage_applied`
  - recovered paths include the release sync implementation and tests.
- `ws_0062f80399b34037b05c989a`
  - status: `running`
  - source workspace: `ws_39eeffb62ae345a8a5e5a0df`
  - salvage event: `workspace.conformance_salvage_applied`
  - recovered paths include `apps/console/lib/format.ts` and
    `apps/console/lib/format.test.mjs`, proving the ignored parent directory did
    not block salvage.

## Residual Risk

- Claude Code print-mode still may emit no agent stdout/stderr until completion.
  The retry workspaces now preserve and continue timed-out work, but a separate
  improvement should make long non-streaming agent processes observable without
  relying only on output bytes.
