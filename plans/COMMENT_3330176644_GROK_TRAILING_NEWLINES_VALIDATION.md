# Comment 3330176644 Grok Trailing Newlines Validation

Plan reference: `COMMENT_3330176644_GROK_TRAILING_NEWLINES_PLAN.md`

## Requirement Status

- Add a regression test that proves Grok launcher prompt delivery preserves
  trailing newlines: Complete.
- Update the launcher to preserve trailing newlines while still keeping prompt
  payloads out of argv before `grok` is executed: Complete.
- Keep Grok CLI flags and model selection behavior unchanged: Complete.
- Run only targeted adapter tests; full AWF/GitHub validation is managed by AWF
  after agent completion: Complete.

## Evidence

Files changed:

- `src/awf/adapters/grok.py`
- `tests/unit/adapters/test_adapters.py`
- `plans/COMMENT_3330176644_GROK_TRAILING_NEWLINES_PLAN.md`
- `plans/COMMENT_3330176644_GROK_TRAILING_NEWLINES_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestGrokAdapter::test_launcher_reads_stdin_and_passes_prompt_to_official_single_flag -q`
  - Failed before implementation because the fake `grok` received
    `workspace prompt` instead of `workspace prompt\n\n`.
- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestGrokAdapter::test_launcher_reads_stdin_and_passes_prompt_to_official_single_flag -q`
  - Passed after implementation: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestGrokAdapter -q`
  - Passed after implementation: `5 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/adapters/grok.py tests/unit/adapters/test_adapters.py`
  - Passed: `All checks passed!`.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/adapters/grok.py tests/unit/adapters/test_adapters.py`
  - Passed: `2 files already formatted`.

Full AWF/GitHub validation was not run in the agent phase per the workspace
contract.
