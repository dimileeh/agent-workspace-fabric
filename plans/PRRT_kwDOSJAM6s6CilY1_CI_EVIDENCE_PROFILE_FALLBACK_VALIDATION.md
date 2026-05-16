# PRRT_kwDOSJAM6s6CilY1 CI Evidence Profile Fallback Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6CilY1_CI_EVIDENCE_PROFILE_FALLBACK_PLAN.md`

## Requirement Status

- Preserve the default safety behavior: Complete.
  - Evidence: existing node-ID-only tests still assert no suggested repro command
    when no fallback commands are supplied.
- Add fallback repro command creation from trusted workspace validation/test
  commands: Complete.
  - Evidence: `tests/unit/runtime/test_ci_failure_evidence.py` now covers a
    node-ID-only log with `pytest_fallback_commands` and gets a focused command.
- Preserve extracted CI pytest command precedence over fallback commands: Complete.
  - Evidence: existing extracted-command tests pass unchanged.
- Keep fallback commands bounded and quoted: Complete.
  - Evidence: `test_ci_failure_evidence_fallback_bounds_and_quotes_multiple_node_ids`
    verifies the existing repro node limit and `shlex.quote` path.
- Pass workspace `test_commands` from the PR monitor into failing-check log
  extraction: Complete.
  - Evidence: `test_fetch_status_supplies_workspace_test_commands_to_ci_log_evidence`
    captures the commands passed from the workspace row to GitHub log fetching.
- Avoid hardcoded AWF-specific command prefixes in generic CI evidence or GitHub
  client code: Complete.
  - Evidence: fallback prefixes are derived from supplied commands; default
    extraction and GitHub fetching still emit no fallback command.

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py tests/unit/common/test_github_client.py tests/unit/runtime/test_pr_monitor_runner.py -q
```

Red result before implementation: failed 4 expected assertions/signatures for
missing `pytest_fallback_commands` support and missing runner command plumbing.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py tests/unit/common/test_github_client.py tests/unit/runtime/test_pr_monitor_runner.py -q
```

Green result after implementation: `225 passed in 44.98s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/ci_failure_evidence.py src/awf/common/github_client.py src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_ci_failure_evidence.py tests/unit/common/test_github_client.py tests/unit/runtime/test_pr_monitor_runner.py tests/unit/runtime/_monitor_runner_fixtures.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: `Success: no issues found in 158 source files`.

## Remaining Gaps

None.
