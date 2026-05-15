# PRRT_CNXS6 Setup Dependency Validation

Plan reference: `plans/PRRT_CNXS6_SETUP_DEPENDENCY_PLAN.md`

## Requirement Status

- Complete: Added a regression test for a compound command where dependency
  install output includes `docker==7.1.0`, then a later bootstrap command emits
  a transient connection timeout.
- Complete: Preserved classification for the existing compound dependency-fetch
  failure where the transient failure line contains package/index evidence.
- Complete: Left deterministic failure handling and secret-redaction paths
  untouched; the full validation unit module still passes.
- Complete: Ran focused verification for the touched classifier behavior.

## Evidence

Files changed:

- `src/awf/runtime/validation.py`
- `tests/unit/runtime/test_validation.py`
- `plans/PRRT_CNXS6_SETUP_DEPENDENCY_PLAN.md`
- `plans/PRRT_CNXS6_SETUP_DEPENDENCY_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_skips_chained_bootstrap_after_package_output -q
```

Result before implementation: failed, reproducing the review-thread issue.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_skips_chained_bootstrap_after_package_output tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_accepts_chained_dependency_output -q
```

Result after implementation: passed, `2 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q
```

Result: passed, `188 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py
```

Result: passed.
