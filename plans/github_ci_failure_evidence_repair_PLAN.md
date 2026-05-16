# GitHub CI Failure Evidence Repair Plan

## Problem Statement And Scope

AWF PR monitors currently fetch failing GitHub Actions logs, but repair prompts
mostly hand agents raw log tails. In PR #238 this caused the agent to spend
cycles rediscovering a known one-test failure by running broad/full coverage
locally. AWF should extract concise, redacted, provider-neutral failure evidence
from failed GitHub Actions checks and pass focused repro guidance into the
repair prompt.

Scope is limited to GitHub CI failure evidence extraction, PR-monitor CI repair
prompts, and monitor observability payloads. This does not change full coverage
policy, PR merge gates, workspace validation strategy, provider recovery, or
scheduler behavior.

## Requirements Checklist

- [ ] Extract structured evidence from `gh run view --log-failed` output:
  failing commands, pytest node IDs, assertion/error snippets, error summaries,
  suggested focused repro commands, and unavailable/partial-log warnings.
- [ ] Redact secrets before evidence is truncated or rendered into prompts.
- [ ] Keep extraction provider-neutral and avoid hardcoding AWF check names such
  as `python-full-coverage`.
- [ ] Preserve current missing-log fallback behavior.
- [ ] Update CI repair prompts so focused repro evidence appears before raw logs.
- [ ] Explicitly tell agents to run focused repro commands first and not run
  broad/full coverage merely to discover an already-known CI failure.
- [ ] Keep all external CI log material quoted as untrusted evidence.
- [ ] Record redacted structured evidence summaries in PR-monitor operation
  payloads.
- [ ] Add TDD regression coverage for single-test, multi-test, non-test,
  unavailable-log, secret-redaction, and provider-neutral check cases.

## Implementation Steps

1. Add failing unit tests in GitHub client, monitor prompt, and PR monitor runner
   coverage around structured CI evidence and prompt behavior.
2. Add a lightweight runtime evidence extractor for GitHub Actions log text.
3. Extend `CheckFailure` with optional evidence fields using safe defaults so
   existing call sites remain compatible.
4. Populate evidence in `GitHubClient.fetch_failing_check_logs` after fetching
   and redacting log text.
5. Update `fix_ci_prompt` to render focused CI evidence before raw log excerpts,
   while keeping raw log excerpts in untrusted evidence blocks.
6. Include evidence summaries in PR-monitor CI repair operation payloads.
7. Create validation documentation and mark each requirement complete/partial.

## Verification Commands And Pass Criteria

Focused tests:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/common/test_github_client.py \
  tests/unit/runtime/test_monitor_prompts.py \
  tests/unit/runtime/test_pr_monitor_runner.py \
  -q
```

Static checks:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

Pass criteria:

- All focused tests pass.
- Ruff and mypy pass.
- Validation file records every requirement as `Complete`, or explicitly
  documents any deferred gap with rationale.
