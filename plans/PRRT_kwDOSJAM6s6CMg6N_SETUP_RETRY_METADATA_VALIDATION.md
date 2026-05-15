# PRRT_kwDOSJAM6s6CMg6N Setup Retry Metadata Validation

Plan reference: `PRRT_kwDOSJAM6s6CMg6N_SETUP_RETRY_METADATA_PLAN.md`

## Requirement Status

- Complete: Updated runtime regression coverage for a setup dependency retry
  followed by a deterministic setup failure.
- Complete: The final runtime command now preserves `setup_dependency_network`
  metadata, attempt lineage, retry count, and non-recovered status.
- Complete: The final command reason remains `COMMAND_FAILED` for the later
  deterministic failure.
- Complete: Added executor coverage proving preserved metadata emits a durable
  retry event without converting the terminal workspace failure into
  `SETUP_DEPENDENCY_NETWORK_FAILURE`.
- Complete: Focused runtime and executor tests plus ruff passed.

## Evidence

Files changed:

- `src/awf/runtime/validation.py`
- `src/awf/control/executor.py`
- `tests/unit/runtime/test_validation.py`
- `tests/unit/control/test_executor_error_paths.py`
- `plans/PRRT_kwDOSJAM6s6CMg6N_SETUP_RETRY_METADATA_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CMg6N_SETUP_RETRY_METADATA_VALIDATION.md`

Regression-first checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_retry_preserves_metadata_when_later_failure_reclassifies -q
```

Result before implementation: failed with missing `setup_dependency_network`
metadata.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths.py::TestExecutorCoverageEdges::test_executor_setup_dependency_retry_then_later_setup_failure_records_retry_without_terminal_setup_reason -q
```

Result before implementation: failed because terminal reason was
`SETUP_DEPENDENCY_NETWORK_FAILURE`.

Final verification:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_retry_preserves_metadata_when_later_failure_reclassifies -q
```

Result: `1 passed in 1.04s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths.py::TestExecutorCoverageEdges::test_executor_setup_dependency_retry_then_later_setup_failure_records_retry_without_terminal_setup_reason -q
```

Result: `1 passed in 2.05s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q
```

Result: `175 passed in 5.94s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths.py::TestExecutorCoverageEdges -q
```

Result: `31 passed in 22.31s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py src/awf/control/executor.py tests/unit/runtime/test_validation.py tests/unit/control/test_executor_error_paths.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/control/executor.py src/awf/runtime/validation.py
```

Result: `Success: no issues found in 2 source files`.

## Gaps

None.
