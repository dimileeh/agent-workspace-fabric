# Review 4303285044 Node Repro Validation

Plan reference: `plans/REVIEW_4303285044_NODE_REPRO_PLAN.md`

## Requirement Status

- Complete: Verify the review finding against current tests and implementation.
  - Evidence: current tests in `tests/unit/runtime/test_ci_failure_evidence.py`
    and `tests/unit/common/test_github_client.py` still expected empty
    `suggested_repro_commands` for extracted pytest node IDs without a trusted
    pytest command.

- Complete: Update the review-called tests to expect the generic fallback
  command while preserving node-ID assertions.
  - Evidence: `tests/unit/runtime/test_ci_failure_evidence.py` now expects the
    generic AWF pytest fallback for a single node ID and exercises generic
    fallback quoting/bounding without configured fallback commands.
  - Evidence: `tests/unit/common/test_github_client.py` now expects a single
    bounded, quoted fallback command for the six-node GitHub log case.

- Complete: Preserve safety around untrusted printed pytest commands.
  - Evidence: `test_does_not_promote_untrusted_printed_pytest_commands` still
    asserts no trusted failing command is extracted, and now verifies the
    suggestion uses the generic AWF prefix rather than the printed untrusted
    `pytest ...; echo owned` line.

- Complete: Use the generic AWF dev pytest prefix.
  - Evidence: `src/awf/runtime/ci_failure_evidence.py` adds
    `_DEFAULT_PYTEST_REPRO_COMMAND = "uv run --python 3.12 --extra dev pytest"`
    as the final fallback when node IDs are present.

- Complete: Bound and quote selected node IDs.
  - Evidence: `_suggest_repro_commands` continues to slice
    `test_node_ids[:_MAX_REPRO_NODES]` and render each selected node ID through
    `shlex.quote`.

- Complete: Validate with focused tests and static checks.
  - Evidence: commands below passed after implementation.

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py tests/unit/common/test_github_client.py -q
```

Result before implementation: failed as expected on four fallback assertions.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py tests/unit/common/test_github_client.py -q
```

Result after implementation: `116 passed in 1.69s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests/unit/runtime/test_ci_failure_evidence.py tests/unit/common/test_github_client.py
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: `Success: no issues found in 158 source files`.

## Remaining Gaps

None.
