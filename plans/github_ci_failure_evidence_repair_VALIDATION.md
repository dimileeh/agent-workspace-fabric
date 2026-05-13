# GitHub CI Failure Evidence Repair Validation

Plan reference: `plans/github_ci_failure_evidence_repair_PLAN.md`

## Requirement Status

- Complete: Extract structured evidence from `gh run view --log-failed` output:
  failing commands, pytest node IDs, assertion/error snippets, error summaries,
  suggested focused repro commands, and unavailable/partial-log warnings.
- Complete: Redact secrets before evidence is truncated or rendered into
  prompts.
- Complete: Keep extraction provider-neutral and avoid hardcoding AWF check
  names such as `python-full-coverage`.
- Complete: Preserve current missing-log fallback behavior.
- Complete: Update CI repair prompts so focused repro evidence appears before
  raw logs.
- Complete: Explicitly tell agents to run focused repro commands first and not
  run broad/full coverage merely to discover an already-known CI failure.
- Complete: Keep all external CI log material quoted as untrusted evidence.
- Complete: Record redacted structured evidence summaries in PR-monitor
  operation payloads.
- Complete: Add TDD regression coverage for single-test, multi-test, non-test,
  unavailable-log, secret-redaction, and provider-neutral check cases.

## Evidence

Files changed:

- `src/awf/runtime/ci_failure_evidence.py`
- `src/awf/runtime/pr_monitor.py`
- `src/awf/common/github_client.py`
- `src/awf/runtime/monitor_prompts.py`
- `src/awf/runtime/pr_monitor_runner.py`
- `tests/unit/common/test_github_client.py`
- `tests/unit/runtime/test_monitor_prompts.py`
- `tests/unit/runtime/test_pr_monitor_runner.py`
- `plans/github_ci_failure_evidence_repair_PLAN.md`
- `plans/github_ci_failure_evidence_repair_VALIDATION.md`

Validation commands run:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/common/test_github_client.py \
  tests/unit/runtime/test_monitor_prompts.py \
  tests/unit/runtime/test_pr_monitor_runner.py \
  -q
```

Result: `231 passed in 102.06s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --extra dev ruff format --check src/awf tests
```

Result: `462 files already formatted`.

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: `Success: no issues found in 153 source files`.

## Remaining Gaps

None.
