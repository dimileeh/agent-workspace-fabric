# PRRT_kwDOSJAM6s6CO6Ag DNS Failure Shapes Validation

Plan reference: `PRRT_kwDOSJAM6s6CO6Ag_DNS_FAILURE_SHAPES_PLAN.md`

## Requirement Status

- Complete: Added regression coverage for npm registry DNS failures reported with
  `ENOTFOUND`.
- Complete: Added regression coverage for git-backed dependency fetch DNS failures
  reported with `Could not resolve host`.
- Complete: Both forms classify as `SETUP_DEPENDENCY_NETWORK_FAILURE` with transient
  category `dns` for recognized dependency setup commands.
- Complete: Existing deterministic-failure filtering and setup-context safeguards
  remain unchanged; the implementation only extends the DNS transient phrase list.

## Evidence

Files changed:

- `src/awf/runtime/validation.py`
- `tests/unit/runtime/test_validation.py`
- `plans/PRRT_kwDOSJAM6s6CO6Ag_DNS_FAILURE_SHAPES_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CO6Ag_DNS_FAILURE_SHAPES_VALIDATION.md`

Commands:

- Before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_accepts_node_transient_error_codes -q`
  failed on the new `ENOTFOUND` and `Could not resolve host` cases.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_accepts_node_transient_error_codes -q`
  passed with 6 tests.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passed with 225 tests.
- After implementation:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  passed.
- After implementation:
  `uv run --python 3.12 --extra dev mypy src/awf`
  passed.
