# CI Evidence Repro Fallback Validation

Plan reference: `plans/CI_EVIDENCE_REPRO_FALLBACK_PLAN.md`

Original AWF contract: `docs/awf-plans/ws_a1b0d9e586c644d1ba4b5d60.md`

## Requirement Status

- Complete: Add failing tests first for fallback repro command creation from pytest node IDs without an extracted pytest command.
  - Evidence: `tests/unit/runtime/test_ci_failure_evidence.py` added `test_ci_failure_evidence_falls_back_to_uv_pytest_for_node_ids_without_command`.
  - Red run: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py -q` failed with empty `suggested_repro_commands`.

- Complete: Add bounded multi-node coverage proving each selected node ID is rendered with `shlex.quote`.
  - Evidence: `tests/unit/runtime/test_ci_failure_evidence.py` added `test_ci_failure_evidence_fallback_bounds_and_quotes_multiple_node_ids`.
  - Red run: same focused evidence test run failed with empty `suggested_repro_commands`.

- Complete: Preserve extracted pytest command behavior.
  - Evidence: existing runtime and GitHub client tests for extracted pytest commands continue to pass.

- Complete: Use the generic AWF dev fallback prefix.
  - Evidence: `src/awf/runtime/ci_failure_evidence.py` now uses `uv run --python 3.12 --extra dev pytest` only when node IDs exist and no trusted pytest command prefix is extracted.

- Complete: Bound fallback commands to the existing maximum repro node count.
  - Evidence: fallback command construction still slices `test_node_ids[:_MAX_REPRO_NODES]`.

- Complete: Avoid hardcoded GitHub Actions check/job names and broad coverage/full-suite suggestions.
  - Evidence: implementation does not inspect check names; fallback commands contain only the dev pytest prefix, known node IDs, and `-q`.

- Complete: Preserve non-test, missing-log, redaction, provider-neutral, prompt-ordering, and payload behavior.
  - Evidence: focused GitHub client, monitor prompt, and PR monitor runner tests pass unchanged except expectations that now follow the fallback behavior.

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py -q
```

Result before implementation: failed as expected on the two new fallback tests.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py -q
```

Result after implementation: `11 passed in 0.53s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py tests/unit/common/test_github_client.py tests/unit/runtime/test_monitor_prompts.py tests/unit/runtime/test_pr_monitor_runner.py -q
```

Result: `263 passed in 54.54s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: `Success: no issues found in 158 source files`.

## Remaining Gaps

None.
