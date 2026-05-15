# Review Comment 4454303511 Dependency Retry Plan

## Problem Statement

Greptile's review-level comment identifies two remaining risks in setup
dependency network retry handling:

1. `_setup_dependency_network_diagnostic` normalizes the full combined
   stdout/stderr before applying the 1000-character diagnostic limit.
2. Unknown setup wrapper commands can be classified as dependency fetch
   failures from package evidence anywhere in output, even when the transient
   network failure belongs to later non-dependency work.

Scope is limited to `src/awf/runtime/validation.py`, focused unit tests, and the
required plan/validation artifacts.

## Requirements Checklist

- [ ] Add a regression test proving diagnostic whitespace normalization receives
      bounded input for pathologically verbose setup output.
- [ ] Bound diagnostic normalization work before running the regex
      substitution, while preserving the existing diagnostic redaction and
      length contract.
- [ ] Add a regression test for unknown wrapper commands where earlier package
      install output is followed by an unrelated DNS failure.
- [ ] Tighten unknown wrapper classification so package/index evidence must be
      co-located with the transient dependency failure, matching compound
      command behavior.
- [ ] Run focused validation for the changed classifier and diagnostic paths.

## Implementation Steps

1. Write failing unit tests in `tests/unit/runtime/test_validation.py` for the
   two review findings.
2. Add a bounded diagnostic scan limit in `src/awf/runtime/validation.py` and
   slice combined output before normalization.
3. Change the unknown-wrapper fallback in `_looks_like_dependency_setup` to use
   `_setup_dependency_output_has_specific_transient_context`.
4. Run the focused tests, then the setup dependency classifier subset if the
   narrow tests pass.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::test_setup_dependency_network_diagnostic_normalization_uses_bounded_input tests/unit/runtime/test_validation.py::test_setup_dependency_network_classifier_skips_unknown_wrapper_after_successful_package_output -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k "setup_dependency_network_classifier or setup_dependency_diagnostics or setup_dependency_network_diagnostic"
```

Pass criteria: all listed tests pass without weakening existing retry,
redaction, or classifier assertions.
