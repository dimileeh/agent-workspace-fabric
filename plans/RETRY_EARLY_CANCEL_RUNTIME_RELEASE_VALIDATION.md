# Retry Early-Cancel Runtime Release Validation

Plan reference: `plans/RETRY_EARLY_CANCEL_RUNTIME_RELEASE_PLAN.md`

## Requirement Status

- Allow retry for a cancelled source with no compose metadata, node placement,
  or reservation evidence: Complete. Added
  `test_retry_allows_early_cancelled_source_without_runtime_evidence`.
- Preserve the existing block for failed legacy null-runtime sources without
  release evidence: Complete. Existing
  `test_retry_rejects_legacy_null_runtime_source_without_reservation` still
  passes.
- Preserve the existing block for real unreleased runtime sources: Complete.
  Existing `test_retry_rejects_host_port_conflict_with_source` still passes.
- Treat the provisioner composite-lock note as verification-only unless current
  code contradicts the accepted behavior: Complete. The provisioner already
  documents the separate short transactions and first-committer-wins behavior;
  no code change was needed there.
- Use focused validation only: Complete. No broad AWF/GitHub-owned validation,
  full coverage, full frontend build, push, or branch change was run.

## Evidence

Files changed:

- `src/awf/service/workspaces_retry.py`
- `tests/unit/service/test_workspace_retry_port.py`
- `plans/RETRY_EARLY_CANCEL_RUNTIME_RELEASE_PLAN.md`
- `plans/RETRY_EARLY_CANCEL_RUNTIME_RELEASE_VALIDATION.md`

Focused checks run:

- Confirmed the new regression failed before the production fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py -k early_cancelled -q`
- Passed targeted safety checks:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py -k "early_cancelled or legacy_null_runtime_source_without_reservation or host_port_conflict_with_source" -q`
- Passed focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces_retry.py tests/unit/service/test_workspace_retry_port.py`
- Passed focused behavioral file:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py -q`

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
