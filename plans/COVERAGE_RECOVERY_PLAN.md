# Coverage Recovery Plan

## Problem Statement And Scope

The full AWF coverage validation completed with passing tests but reported
98.57% total coverage against the required 99.00% threshold. The scope of this
pass is to add small, meaningful regression coverage for existing uncovered
paths, without changing production behavior or validation policy.

## Requirements Checklist

- Preserve the existing quality gates and coverage threshold.
- Do not change AWF branch management, push, rebase, or switch branches.
- Prefer focused tests for real existing behavior over broad rewrites.
- Keep changes scoped to coverage recovery and any required plan/validation
  artifacts.
- Run the targeted validation commands that will be used on the next pass.

## Implementation Steps

1. Inspect existing coverage data and identify uncovered, low-risk paths that
   are reachable through unit tests.
2. Add focused tests for the selected behavior, following existing test style.
3. Run the narrowest relevant tests first, then the listed validation commands.
4. Create `plans/COVERAGE_RECOVERY_VALIDATION.md` with requirement status and
   command evidence.
5. Commit the local changes on the current AWF-owned branch.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev ruff check src/awf/cli tests/unit/cli`
- `uv run --python 3.12 --extra dev mypy src/awf/cli`
- `uv run --python 3.12 --extra dev pytest tests/unit/cli -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_test_quality_guardrails_self.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_pr_monitor_adoption.py tests/unit/service/test_pr_monitor_adoption.py tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py -q`

Pass criteria: all listed commands pass, and any added tests assert meaningful
behavior on previously uncovered paths.
