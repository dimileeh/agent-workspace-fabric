# PRRT_kwDOSJAM6s6Ci7MN Validation

Plan reference: `PRRT_kwDOSJAM6s6Ci7MN_PLAN.md`

## Requirement Status

- Add a regression test showing a fallback command with shell setup before
  `pytest` produces an executable focused repro suggestion: Complete.
- Preserve existing behavior for ordinary pytest commands, including dropping
  broad original pytest arguments before appending focused node IDs: Complete.
- Avoid weakening existing regression tests or safety assertions: Complete.
- Run the narrow unit test file for CI failure evidence: Complete.

## Evidence

- Changed `tests/unit/runtime/test_ci_failure_evidence.py` with a regression
  for `cd services/api && pytest --maxfail=1`.
- Changed `src/awf/runtime/ci_failure_evidence.py` so pytest repro extraction
  returns the original shell prefix up to `pytest` or `python -m pytest`.
- Confirmed the new regression failed before implementation with
  `cd services/api '&&' pytest ...`.
- Ran `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py -q`
  and it passed.
- Ran `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py tests/unit/common/test_github_client.py -q`
  and it passed.
- Ran `uv run --python 3.12 --extra dev ruff check src/awf/runtime/ci_failure_evidence.py tests/unit/runtime/test_ci_failure_evidence.py`
  and it passed.
- Ran `uv run --python 3.12 --extra dev mypy src/awf/runtime/ci_failure_evidence.py`
  and it passed.

## Gaps

None.
