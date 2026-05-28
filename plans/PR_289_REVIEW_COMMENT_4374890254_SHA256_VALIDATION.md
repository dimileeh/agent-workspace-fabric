# PR 289 Review Comment 4374890254 SHA256 Validation

Plan reference: `plans/PR_289_REVIEW_COMMENT_4374890254_SHA256_PLAN.md`

## Requirement Status

- Complete: The GitHub CLI install block still downloads the pinned
  `GH_VERSION` release asset and matching checksum file using `gh_deb` and
  `GH_VERSION`.
- Complete: The Dockerfile now extracts the expected checksum, computes the
  local `.deb` checksum, and compares them directly.
- Complete: Missing checksum entries and hash mismatches both exit nonzero
  before `apt-get install`.
- Complete: Temporary `.deb` and checksum files remain covered by the existing
  trap cleanup.
- Complete: `gh --version` still runs after the local package install.
- Complete: Only focused local checks were run. Full workflow, image-build, and
  coverage validation remain owned by AWF/GitHub after agent completion.

## Evidence

Files changed:

- `docker/agent-runtime.Dockerfile`
- `tests/unit/test_agent_runtime_dockerfile.py`
- `plans/PR_289_REVIEW_COMMENT_4374890254_SHA256_PLAN.md`
- `plans/PR_289_REVIEW_COMMENT_4374890254_SHA256_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  after the test-only change failed as expected:
  `1 failed, 5 passed in 0.40s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  after the Dockerfile change and final assertion update passed:
  `6 passed in 0.38s`.
- `uv run --python 3.12 --extra dev ruff check tests/unit/test_agent_runtime_dockerfile.py`
  passed.
- `uv run --python 3.12 --extra dev ruff format --check tests/unit/test_agent_runtime_dockerfile.py`
  passed.
- `git diff --check` passed.

## Gaps

No plan requirements remain partial or missing. I intentionally did not run a
Docker image build, full unit suite, full coverage gate, or workflow-equivalent
validation locally because the AWF workspace contract assigns broad validation
provenance to AWF/GitHub after the agent phase.
