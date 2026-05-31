# Comment 4396834428 Validation

Plan reference: `COMMENT_4396834428_PLAN.md`

## Requirement Status

- Complete: Verify existing Dockerfile Cursor installer wiring remains non-root
  usable and fail-fast.
  Evidence: `docker/agent-runtime.Dockerfile` already installs Cursor under
  `/opt/cursor`, copies `cursor-agent` to `/usr/local/bin`, and validates
  `cursor-agent --version` after `USER agent`.
- Complete: Verify existing Cursor provider readiness checks include runtime CLI
  probing.
  Evidence: `src/awf/service/provider_readiness.py` already routes Cursor through
  `_check_cursor_readiness`, probes `command -v cursor-agent`, and includes
  `runtime_cli_probe` in Cursor readiness output.
- Complete: Tighten Cursor provider inference so a bare `cursor` substring does
  not attribute unrelated output to Cursor.
  Evidence: `_CURSOR_MARKERS` no longer contains bare `cursor`, and
  `test_cursor_provider_inference_requires_specific_cursor_marker` covers the
  regression.
- Complete: Remove the generic `please authenticate` auth marker unless another
  provider-specific signal is present.
  Evidence: `_AUTH_FAILURE_MARKERS` no longer contains `please authenticate`, and
  `test_generic_auth_prompt_with_cursor_word_is_not_provider_failure` covers the
  regression.
- Complete: Preserve concrete Cursor auth classification.
  Evidence: Existing Cursor auth tests still pass, and the new inference test
  covers `cursor auth` and `cursor api key`.
- Complete: Clarify the release-readiness provider filter example.
  Evidence: `docs/REST_API_REFERENCE.md` now shows repeated provider filters for
  `claude_code` and `cursor` plus a note explaining the Cursor example.

## Verification Evidence

- Initial TDD failure, before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_provider_failures.py -q`
  failed with the two new Cursor marker regression tests.
- Passing focused adapter tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_provider_failures.py -q`
  returned `13 passed`.
- Passing focused Cursor Dockerfile/readiness tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py -q -k "cursor"`
  returned `4 passed, 52 deselected`.
- Passing focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/adapters/provider_failures.py tests/unit/adapters/test_provider_failures.py`
  returned `All checks passed!`.

Full AWF/GitHub validation was not run in this agent phase; AWF owns the broad
validation suite and merge-gating provenance after completion.
