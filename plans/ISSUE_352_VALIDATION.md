# Issue #352 Validation

Plan reference: `plans/ISSUE_352_PLAN.md`

## Requirement Status

- Complete: Base-branch ruleset merge methods are fetched from `repos/{repo}/rules/branches/{branch}` and intersected with repo-level merge flags.
- Complete: The monitor passes an explicit effective merge method to `merge_pr`; `squash` remains preferred when allowed.
- Complete: A base constrained to merge commits uses `method="merge"` and does not attempt squash.
- Complete: Unconstrained bases with repo squash enabled still use `method="squash"`.
- Complete: Method-disallowed GraphQL rejections are classified specifically for squash, merge-commit, and rebase messages.
- Complete: A method rejection retries once with the next effective allowed method and succeeds when that alternative works.
- Complete: A method rejection with no effective alternative records the existing `GITHUB_MERGE_FAILED` failure for the merge attempt, persists a current-head merge-method blocker, and routes through `NotifyHuman` instead of the transient retry loop.
- Complete: Empty/no `pull_request` branch ruleset responses are treated as unconstrained; multiple rules are combined conservatively.
- Complete: A `pull_request` branch rule whose parameters omit `allowed_merge_methods` is documented and asserted as unconstrained, so the runner falls back to repo-level merge flags.
- Complete: Branch-rules `gh api` failure stderr and malformed JSON stdout are redacted through `GitHubClientError` diagnostics before they can become log/audit evidence.
- Complete: Upstream merge short-circuit already exists. `decide()` returns `ShortCircuitCompleted` for merged PRs, and `tests/unit/runtime/test_pr_monitor_manual_merge.py` covers a live runner sequence ending in `ShortCircuitCompleted`; no implementation change was needed.

## Evidence

Files changed:

- `src/awf/common/github_client.py`
- `src/awf/runtime/monitor_state_keys.py`
- `src/awf/runtime/pr_monitor.py`
- `src/awf/runtime/pr_monitor_runner/merge_loop.py`
- `tests/unit/common/test_github_client_parts/test_github_client_part_004.py`
- `tests/unit/runtime/_monitor_runner_fixtures.py`
- `tests/unit/runtime/test_pr_monitor_merge_methods.py`

Iteration 1 conformance checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py -q
```

Initial result after adding the redaction regressions: failed as expected because branch-rules `GitHubClientError.stderr` still contained the fake provider-token marker from failing `gh api` stderr and malformed JSON stdout.

Final result after redacting GitHub client error diagnostics: `44 passed in 0.74s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q
```

Result: `6 passed in 7.61s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client_parts/test_github_client_part_004.py tests/unit/runtime/test_pr_monitor_merge_methods.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/common/github_client.py
```

Result: `Success: no issues found in 1 source file`.

After formatting the touched Python files, the combined focused verification was rerun:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py tests/unit/runtime/test_pr_monitor_merge_methods.py -q
```

Result: `50 passed in 8.33s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/unit/common/test_github_client_parts/test_github_client_part_004.py tests/unit/runtime/test_pr_monitor_merge_methods.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev ruff format --check src/awf/common/github_client.py tests/unit/common/test_github_client_parts/test_github_client_part_004.py
```

Result: `2 files already formatted`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/common/github_client.py
```

Result: `Success: no issues found in 1 source file`.

Earlier implementation checks:

Focused commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py tests/unit/common/test_github_client_parts/test_github_client_part_004.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_002.py::test_transient_github_merge_error_retries_without_human_escalation tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_002.py::test_non_transient_github_merge_error_records_failed_audit_and_redacts -q
```

Result: `49 passed in 10.73s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py src/awf/runtime/monitor_state_keys.py src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/_monitor_runner_fixtures.py tests/unit/runtime/test_pr_monitor_merge_methods.py tests/unit/common/test_github_client_parts/test_github_client_part_004.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev ruff format --check src/awf/common/github_client.py src/awf/runtime/monitor_state_keys.py src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/_monitor_runner_fixtures.py tests/unit/runtime/test_pr_monitor_merge_methods.py tests/unit/common/test_github_client_parts/test_github_client_part_004.py
```

Result: `7 files already formatted`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/common/github_client.py src/awf/runtime/monitor_state_keys.py src/awf/runtime/pr_monitor.py src/awf/runtime/pr_monitor_runner/merge_loop.py
```

Result: `Success: no issues found in 4 source files`.

## Broad Validation

The task text included a full local gate request, but this AWF workspace contract forbids broad CI-equivalent validation during the agent phase. Full AWF/GitHub validation, provenance, timeouts, and merge gating remain owned by AWF after agent completion.

## Remaining Gaps

None identified.
