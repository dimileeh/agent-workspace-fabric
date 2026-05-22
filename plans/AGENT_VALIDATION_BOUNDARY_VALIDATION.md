# Agent Validation Boundary Validation

Plan reference: `plans/AGENT_VALIDATION_BOUNDARY_PLAN.md`

## Requirement Status

- Agents are explicitly told not to run full `.awf/workspace.yml` validation,
  full coverage gates, whole-repo suites, or full frontend builds inside the
  agent phase: Complete.
  - Added rule 4 to `_AWF_PROMPT_PREAMBLE` in `src/awf/adapters/base.py`.
- Agents are told to run focused tests/lint only when useful for changed files
  or behavior: Complete.
  - Added rule 5 to `_AWF_PROMPT_PREAMBLE`.
- Agents are told validation docs may describe AWF/GitHub-managed validation
  without executing it themselves: Complete.
  - Added explicit validation-document guidance to rule 5.
- The instruction is injected by AWF for every adapter run: Complete.
  - `_AWF_PROMPT_PREAMBLE` is prepended in `AgentAdapter.run()`, shared by
    Claude Code, Codex, Gemini, and OpenCode.
- Current runaway coverage subprocesses are stopped: Complete.
  - Killed only the nested `pytest --cov --cov-fail-under=99` subprocess groups
    in `ws_6f996a0f4db54b93814ff6bc` and `ws_0062f80399b34037b05c989a`.
  - Cancelled those old-boundary workspaces so Claude could not continue from
    the ambiguous prompt.
- Unit tests prove the preamble contains the validation boundary: Complete.
  - Updated `_assert_prompt_sent_on_stdin()` in
    `tests/unit/adapters/test_adapters.py`.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py -q
# 42 passed

uv run --python 3.12 --extra dev ruff check \
  src/awf/adapters/base.py tests/unit/adapters/test_adapters.py
# All checks passed

uv run --python 3.12 --extra dev ruff format --check \
  src/awf/adapters/base.py tests/unit/adapters/test_adapters.py
# 2 files already formatted

uv run --python 3.12 --extra dev mypy src/awf/adapters/base.py
# Success: no issues found in 1 source file
```

## Live AWF Evidence

- Rebuilt/recreated the local AWF API and worker.
- Verified `/healthz` is OK.
- Verified inside `awf-local-service-api-1` that `_AWF_PROMPT_PREAMBLE` contains
  both `broad validation` and `pytest --cov`.
- Cancelled old-boundary salvage retries:
  - `ws_6f996a0f4db54b93814ff6bc`
  - `ws_0062f80399b34037b05c989a`
- Created new bounded retries:
  - `ws_2a4292d5b79747c7b93f881e` from release-task salvage
  - `ws_983f7e6969124e52b77c53e4` from LLM-usage salvage
- Updated heartbeat automation `check-awf-salvage-retries` to watch the new
  workspace IDs and explicitly alert on forbidden broad validation commands.

## Residual Risk

- The new bounded retries are still in progress. The heartbeat monitor now
  checks for accidental broad validation in process trees, but final proof is
  that the agents finish without spawning full coverage/build suites and then
  AWF/GitHub validation owns the broad gates.
