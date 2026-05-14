# PRRT_kwDOSJAM6s6CNPD Setup Dependency Evidence Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6CNPD_SETUP_DEPENDENCY_EVIDENCE_PLAN.md`

## Requirement Status

- Complete: Added a regression test for a standalone unrecognized setup script
  reporting a generic fetch timeout without package, index, or known dependency
  host evidence.
- Complete: Preserved retry classification for recognized dependency tools,
  covered by existing parameterized install-tool tests.
- Complete: Preserved retry classification for unrecognized scripts with
  specific package/index/known-host evidence.
- Complete: Kept deterministic failures and transient category behavior intact,
  covered by the full `tests/unit/runtime/test_validation.py` pass.

## Evidence

Files changed:

- `src/awf/runtime/validation.py`
- `tests/unit/runtime/test_validation.py`
- `plans/PRRT_kwDOSJAM6s6CNPD_SETUP_DEPENDENCY_EVIDENCE_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CNPD_SETUP_DEPENDENCY_EVIDENCE_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_skips_standalone_bootstrap_fetch_failure -q`
  initially failed before the implementation, proving the regression.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_skips_standalone_bootstrap_fetch_failure tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_accepts_install_package_manager_verbs tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_retries_dependency_simple_index_fallback -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passed with 187 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  passed.

## Gaps

None.
