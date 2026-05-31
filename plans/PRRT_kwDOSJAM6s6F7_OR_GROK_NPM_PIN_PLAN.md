# PRRT_kwDOSJAM6s6F7-or Grok Npm Pin Plan

## Problem Statement And Scope

The agent-runtime Dockerfile currently attempts to pin the Grok CLI by passing
`GROK_VERSION` to the xAI shell installer. The review thread notes that this
argument is not publicly documented and may be ignored, which would leave Grok
as an unpinned runtime dependency. This fix is scoped to installing the official
`@xai-official/grok` npm package at the existing `GROK_VERSION` pin.

## Requirements Checklist

- Update the Dockerfile regression test to require
  `@xai-official/grok@${GROK_VERSION}` and reject the unversioned shell
  installer path.
- Update `docker/agent-runtime.Dockerfile` so Grok is installed by the same
  pinned npm install command used for the other Node-based CLIs.
- Preserve the existing `grok --version` smoke check.
- Run focused checks only; AWF/GitHub owns full image builds, coverage gates,
  and broad validation after the agent phase.

## Implementation Steps

1. Change `tests/unit/test_agent_runtime_dockerfile.py` so the current
   Dockerfile fails the Grok pinning contract.
2. Run the focused Dockerfile test and capture the expected red result.
3. Add `@xai-official/grok@${GROK_VERSION}` to the pinned npm install command
   and remove the separate shell-installer layer.
4. Re-run the focused Dockerfile test and targeted lint for the touched test.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_all_supported_coding_clis -q`
  must fail before the Dockerfile change and pass after it.
- `uv run --python 3.12 --extra dev ruff check tests/unit/test_agent_runtime_dockerfile.py`
  must pass.
- Full repository tests, coverage gates, frontend builds, Docker image builds,
  and CI-equivalent checks are intentionally left to AWF/GitHub.
