# CI Evidence Extractor Hang Plan

## Problem Statement

An adopted PR monitor for `dimileeh/aira-agent#480` resumed but did not emit a
`monitor.action` event or start an agent. The worker process became CPU-bound
while handling the monitor, leaving the workspace apparently alive but making no
progress.

Investigation reproduced the hot path locally: fetching PR status and monitor
decision were fast, while failed-check log evidence extraction hung inside
pytest node-id extraction for real GitHub Actions output.

## Scope

- Fix only the CI failure evidence parser used by PR monitor failed-check
  evidence collection.
- Preserve existing behavior for focused pytest repro command generation,
  quoting, parameterized node ids, redaction, and evidence summarization.
- Do not change monitor policy or PR action selection logic.

## Requirements

- Replace the nested pytest node-id regex path with bounded, linear parsing.
- Add regression coverage for malformed or noisy CI lines that previously could
  monopolize the worker before a monitor action was logged.
- Keep extraction of normal, nested, and parameterized pytest node ids intact.
- Verify against the captured PR #480 failed-check log.
- Rebuild/restart the local AWF worker and remonitor `ws_206ffcf5c60a46ccad56fbe0`.

## Implementation Steps

1. Add focused regression tests in `tests/unit/runtime/test_ci_failure_evidence.py`.
2. Implement a linear pytest node-id scanner in `src/awf/runtime/ci_failure_evidence.py`.
3. Run focused unit tests and lint/type checks for the touched runtime module.
4. Reproduce extraction against the captured failed CI log.
5. Rebuild/restart AWF control-plane services and trigger remonitor for the stuck
   workspace.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py -q
uv run --python 3.12 --extra dev ruff check src/awf/runtime/ci_failure_evidence.py tests/unit/runtime/test_ci_failure_evidence.py
uv run --python 3.12 --extra dev mypy src/awf/runtime/ci_failure_evidence.py
```

Pass criteria: focused tests pass, the captured PR #480 log extracts evidence
quickly, and the remonitored workspace emits a monitor action or starts the PR
agent instead of leaving the worker CPU-bound.
