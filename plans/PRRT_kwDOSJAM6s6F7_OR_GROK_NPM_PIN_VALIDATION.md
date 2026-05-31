# PRRT_kwDOSJAM6s6F7-or Grok Npm Pin Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F7_OR_GROK_NPM_PIN_PLAN.md`

## Requirement Status

- Complete: Updated the Dockerfile regression test to require
  `@xai-official/grok@${GROK_VERSION}` and reject the xAI shell installer path.
- Complete: Updated `docker/agent-runtime.Dockerfile` so Grok installs in the
  pinned npm install transaction with the other coding CLIs.
- Complete: Preserved the `grok --version` smoke check after npm installation.
- Complete: Ran focused task checks; the local commit hook also ran and passed
  during `git commit`. Full AWF/GitHub validation remains owned by AWF after
  agent completion.

## Evidence

- Package availability check:
  `npm view @xai-official/grok@0.2.14 version` returned `0.2.14`.
- Package binary check:
  `npm view @xai-official/grok@0.2.14 bin --json` returned a `grok` binary
  mapping.
- Package platform check:
  `npm view @xai-official/grok@0.2.14 os cpu dependencies optionalDependencies --json`
  listed Linux support for `x64` and `arm64` with pinned platform packages.
- Red test before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_all_supported_coding_clis -q`
  failed because `@xai-official/grok@${GROK_VERSION}` was absent.
- Passing focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_all_supported_coding_clis -q`
  passed with `1 passed`.
- Passing Dockerfile contract file:
  `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  passed with `7 passed`.
- Passing focused lint:
  `uv run --python 3.12 --extra dev ruff check tests/unit/test_agent_runtime_dockerfile.py`
  passed.
- Local commit hook:
  `git commit` ran the configured pre-commit hooks and they passed before
  creating the local review-thread commit.

## Remaining Gaps

No known gap for this review thread. Full Docker image rebuilds, coverage gates,
and AWF/GitHub CI-equivalent validation were intentionally not run inside the
agent phase per the AWF workspace contract.
