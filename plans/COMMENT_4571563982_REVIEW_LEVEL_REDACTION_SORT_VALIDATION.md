# Comment 4571563982 Review-Level Redaction and Sort Validation

Plan reference:
`plans/COMMENT_4571563982_REVIEW_LEVEL_REDACTION_SORT_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving first-run pretty output renders
  redacted collision suffixes in numeric order once suffixes reach `#10`.
- Complete: Added regression coverage proving `redact_secrets` redacts
  generic `*_TOKEN`, `PASSWORD`, `PASSWD`, `SECRET`, `*_API_KEY`, and
  `*_ACCESS_KEY` assignment-style secrets.
- Complete: Implemented numeric-aware pretty mapping sorting for generated
  `#N` suffixes without changing JSON collision key preservation.
- Complete: Moved the audit assignment regex into
  `awf.common.token_patterns.compile_token_assignment_re()` and reused it from
  both audit and operator log redaction.
- Complete: Ran targeted validation only. Full AWF/GitHub validation and
  coverage gates remain managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/common/token_patterns.py`
- `src/awf/common/audit.py`
- `src/awf/common/redaction.py`
- `src/awf/host_setup/rendering.py`
- `tests/unit/service/test_host_setup_rendering.py`
- `tests/unit/runtime/test_log_redaction.py`

Failing-regression confirmation before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_pretty_sorts_redacted_collision_suffixes_numerically tests/unit/runtime/test_log_redaction.py::test_redact_secrets_handles_token_assignments_and_bearer_values -q
```

Result: failed with the new pretty suffix ordering regression and the newly
added `redact_secrets` assignment cases.

Passing focused validation after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_pretty_sorts_redacted_collision_suffixes_numerically tests/unit/runtime/test_log_redaction.py::test_redact_secrets_handles_token_assignments_and_bearer_values -q
```

Result: `14 passed in 0.49s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py tests/unit/runtime/test_log_redaction.py tests/unit/common/test_audit.py -q
```

Result: `61 passed in 0.76s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/token_patterns.py src/awf/common/audit.py src/awf/common/redaction.py src/awf/host_setup/rendering.py tests/unit/service/test_host_setup_rendering.py tests/unit/runtime/test_log_redaction.py tests/unit/common/test_audit.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/common/token_patterns.py src/awf/common/audit.py src/awf/common/redaction.py src/awf/host_setup/rendering.py
```

Result: `Success: no issues found in 4 source files`.
