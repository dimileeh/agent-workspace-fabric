# Review Issue 4590903660 Repo Flags Transient Plan

## Problem Statement And Scope

Greptile flagged that `fetch_repo_merge_methods` raises a missing-merge-flags
`GitHubClientError` with a successful-looking return code and no transient
marker. The PR monitor then treats an anomalously partial GitHub repository
payload as permanent and records a merge-method blocker for the current head
SHA.

Scope is limited to the repository merge-method flag anomaly path and focused
unit coverage for that behavior.

## Requirements Checklist

- Missing repository merge-method flags must remain an error, not an empty
  policy.
- The error must be distinguishable from a successful command result.
- The error text must be recognizable by the existing transient GitHub
  classifier.
- Existing explicit false merge flags must still return an empty tuple.
- Run only focused local checks; full AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Update the missing-flags error in `src/awf/common/github_client.py`.
2. Update focused GitHub client unit assertions for missing and partial flags.
3. Add a focused PR monitor transient-classifier assertion for the generated
   missing-flags message.
4. Add validation notes with evidence and any gaps.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py -q -k fetch_repo_merge_methods`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_001.py -q -k transient_github_error_classifier_keeps_auth_errors_terminal`

Pass criteria: targeted tests pass and verify missing/partial payloads surface
as retryable API anomalies without changing explicit false-policy behavior.
