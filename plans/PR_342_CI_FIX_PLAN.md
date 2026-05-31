# PR #342 CI Fix Plan

## Problem Statement and Scope

PR #342 fails the `python-full-coverage` and `release-artifacts` GitHub Actions
jobs before Python coverage or artifact validation can complete. Both failures
come from the `docker/agent-runtime.Dockerfile` agent-runtime image build:
`cursor-agent --version` exits with code 127 because the installed Cursor
wrapper tries to run `/usr/local/bin/node`, while the NodeSource package exposes
Node elsewhere on PATH.

This fix is limited to the Cursor agent-runtime image build and focused tests
that guard the Dockerfile contract. It also keeps existing Cursor adapter and
provider-readiness review-comment fixes intact.

## Requirements Checklist

- Preserve the Cursor CLI installer path and do not weaken the `cursor-agent
  --version` smoke checks.
- Make `/usr/local/bin/node` available in the agent-runtime image before
  `cursor-agent` is executed.
- Add or update focused regression coverage for the Dockerfile behavior.
- Run only focused local checks; full AWF/GitHub validation is managed by AWF
  after agent completion.
- Commit the local fix without pushing or changing branches.

## Implementation Steps

1. Add a Dockerfile step after NodeSource installs Node to symlink the resolved
   `node` binary to `/usr/local/bin/node` when that path is absent.
2. Update `tests/unit/test_agent_runtime_dockerfile.py` to assert the symlink
   guard exists before the Cursor installer and smoke checks.
3. Run focused tests for the Dockerfile and the Cursor readiness/adapter areas
   touched by recent CI/review evidence.
4. Record validation evidence in `plans/PR_342_CI_FIX_VALIDATION.md`.
5. Commit the fix locally with a conventional `fix(ci): ...` message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestCursorAdapter tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py::test_provider_readiness_cursor_env_present -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check docker/agent-runtime.Dockerfile tests/unit/test_agent_runtime_dockerfile.py`
  is attempted only if ruff accepts this file selection; otherwise record why it
  was not applicable.
- Full coverage, full repository tests, frontend builds, and CI-equivalent
  image validation are intentionally left to AWF/GitHub per the workspace
  contract.

## Assumptions/Changes

### Iteration 2: Cursor launcher bundle path

The latest CI run for PR #342 still fails in the agent-runtime Docker build, but
the observed root cause has moved past `/usr/local/bin/node`. The Cursor
installer creates `~/.local/bin/cursor-agent` as a symlink to a versioned bundle
launcher. That launcher resolves `realpath "$0"` and expects its bundled
`index.js` next to the resolved script. Copying the launcher into
`/usr/local/bin/cursor-agent` makes it look for `/usr/local/bin/index.js`, which
does not exist.

Additional requirements:

- Preserve strict `cursor-agent --version` smoke checks.
- Expose `cursor-agent` on the system PATH without copying the bundle launcher
  away from its versioned directory.
- Add a focused regression assertion that the Dockerfile uses a symlink from
  `/usr/local/bin/cursor-agent` to the installer-managed launcher.

Additional implementation steps:

1. Update the Dockerfile unit test to expect a symlinked Cursor launcher and to
   reject copying/installing the launcher into `/usr/local/bin`.
2. Confirm that test fails against the current Dockerfile.
3. Replace the Dockerfile copy/install step with `ln -sf "$cursor_path"
   /usr/local/bin/cursor-agent`.
4. Run the focused Dockerfile unit test and record results in validation.
