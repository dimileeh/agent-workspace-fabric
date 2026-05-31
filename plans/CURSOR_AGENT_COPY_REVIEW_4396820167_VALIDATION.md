# Cursor Agent Copy Review 4396820167 Validation

Plan reference:
`plans/CURSOR_AGENT_COPY_REVIEW_4396820167_PLAN.md`

## Requirement Status

- Complete: Updated the Dockerfile regression test first to require
  `install -m 0755 "$cursor_path" /usr/local/bin/cursor-agent` and reject
  `ln -sf "$cursor_path" /usr/local/bin/cursor-agent`.
- Complete: Confirmed the updated focused test failed against the unchanged
  Dockerfile.
- Complete: Replaced the symlink with a copied executable in
  `docker/agent-runtime.Dockerfile` while keeping the installer location check
  and non-root `agent` smoke check.
- Complete: Ran only focused validation. Full AWF/GitHub validation remains
  managed by AWF after agent completion.
- Complete: Prepared this validation artifact before committing the local fix.

## Evidence

Files changed:

- `docker/agent-runtime.Dockerfile`
- `tests/unit/test_agent_runtime_dockerfile.py`
- `plans/CURSOR_AGENT_COPY_REVIEW_4396820167_PLAN.md`
- `plans/CURSOR_AGENT_COPY_REVIEW_4396820167_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q -k cursor`
  - Result: exited 5 with `7 deselected`; selector was not useful because no
    test name contains `cursor`.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_all_supported_coding_clis -q`
  - Result after test-only edit: failed because the Dockerfile did not yet
    contain `install -m 0755 "$cursor_path" /usr/local/bin/cursor-agent`.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_all_supported_coding_clis -q`
  - Result after Dockerfile fix: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  - Result: passed, `7 passed`.
- `uv run --python 3.12 --extra dev ruff check tests/unit/test_agent_runtime_dockerfile.py`
  - Result: passed.

## Remaining Gaps

None for this review comment. A Docker image rebuild and full repository
validation were intentionally not run in this agent phase because AWF/GitHub
own broad validation, provenance, logs, and merge gating after agent
completion.
