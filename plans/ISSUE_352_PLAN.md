# Issue #352 Implementation Plan

## Scope

Resolve GitHub issue #352: the PR monitor currently attempts `gh pr merge --squash` by default, ignores base-branch `allowed_merge_methods`, and treats permanent merge-method rejections as transient retryable failures.

This plan intentionally covers implementation work only after the planning phase. During this phase, only this configured plan artifact is created or updated.

## Intended Files and Modules to Touch

- `src/awf/common/github_client.py`
  - Add a GitHub client method to resolve branch ruleset merge-method constraints for a base branch using `gh api repos/{repo}/rules/branches/{branch}`.
  - Reuse existing `gh api` invocation, JSON parsing, logging, redaction, and `GitHubClientError` patterns.
  - Return a normalized unconstrained/constrained result rather than hard-coding repository or branch names.

- `src/awf/runtime/pr_monitor_runner/merge_loop.py`
  - Fetch repo-level merge flags and base-branch ruleset constraints before calling `merge_pr`.
  - Compute the effective allowed merge methods.
  - Preserve `squash` as the preferred default when it is effective-allowed.
  - Pass `method=<chosen>` explicitly to `merge_pr`.
  - Detect permanent merge-method rejection errors from `GitHubClientError` messages.
  - Retry once with the next effective-allowed method when available.
  - Escalate to the existing `NotifyHuman` path when no effective method remains or method resolution is empty/unresolvable.
  - Preserve existing `GITHUB_MERGE_FAILED` event/audit reason-code flow for failed merge attempts.
  - Check whether upstream/external merged PR detection already short-circuits to `ShortCircuitCompleted`; add only the minimal detection if absent.

- `src/awf/runtime/pr_monitor.py`
  - Touch only if needed to carry a normalized action/reason for merge-method mismatch through the existing `NotifyHuman` model.
  - Keep the pure decision core I/O-free.

- Existing focused test modules under `tests/unit/` for GitHub client and PR monitor runner behavior
  - Exact files will be selected by matching existing test layout after inspecting the suite during implementation.
  - Expected likely areas: GitHub client tests for `gh api` parsing and merge-loop runner tests for merge method selection, retry, and escalation.

- `plans/ISSUE_352_PLAN.md`
  - The task also requires this saved implementation plan before coding. In the implementation phase, create it with the same substance as this AWF-configured planning artifact.

- `plans/ISSUE_352_VALIDATION.md`
  - Create after implementation with focused evidence, gaps, and confirmation that broad AWF/GitHub validation is owned by AWF after agent completion.

## Tests to Write First

Follow strict TDD. Add or update failing regression tests before implementation.

1. Base branch ruleset allows only merge commits
   - Arrange a PR whose base branch ruleset has `allowed_merge_methods: ["merge"]` while repo-level flags still allow squash.
   - Assert the monitor passes `method="merge"` to `merge_pr`.
   - Assert it never calls `merge_pr` with `method="squash"` for that constrained base branch.

2. Squash remains default when unconstrained and repo allows squash
   - Arrange no constraining pull_request ruleset or an empty ruleset response.
   - Arrange repo-level `allow_squash_merge=true`.
   - Assert the monitor passes `method="squash"` explicitly.

3. Permanent method rejection with no allowed alternative escalates
   - Arrange chosen method failure with a `GitHubClientError` message such as `GraphQL: Squash merges are not allowed on this repository.`.
   - Arrange no remaining effective-allowed method.
   - Assert the result transitions to existing `NotifyHuman` behavior with a clear merge-method mismatch reason.
   - Assert the path does not re-enter the transient retry loop.
   - Assert the existing `GITHUB_MERGE_FAILED` event/audit reason is still recorded for the failed attempt.

4. Permanent method rejection with an allowed alternative retries once and succeeds
   - Arrange effective methods with a first preferred method and one alternative.
   - Make the first `merge_pr(method=<first>)` raise a method-disallowed `GitHubClientError`.
   - Make the second `merge_pr(method=<alternative>)` succeed.
   - Assert exactly two merge calls and the second uses the alternative.
   - Assert no human notification is emitted on success.

5. Ruleset and intersection coverage
   - GitHub client parsing tests for:
     - empty ruleset endpoint response: unconstrained;
     - ruleset response without a `pull_request` rule: unconstrained;
     - `pull_request` rule without `allowed_merge_methods`: unconstrained or documented unresolvable behavior, matching implementation;
     - `pull_request` rule with `merge`, `squash`, `rebase`: normalized set;
     - malformed or failing `gh api` response raises/preserves `GitHubClientError` without token leakage.
   - Pure helper tests, if helper is introduced, for:
     - constrained ruleset intersected with repo-level flags;
     - no ruleset constraint falling back to repo-level flags;
     - empty intersection causing NotifyHuman/unresolvable path;
     - preferred ordering with `squash` first when allowed, then deterministic alternatives.

6. Upstream merge short-circuit check
   - Inspect existing tests and behavior for externally merged PRs.
   - If already covered, do not add implementation scope; note it in validation.
   - If missing, add a focused test that an already-merged PR produces `ShortCircuitCompleted` and does not attempt another merge.

## Implementation Approach

1. Inspect existing GitHub client and merge-loop tests to identify local patterns and fixtures.
2. Add failing tests for the required scenarios above.
3. Add a small normalized merge-method model/helper only if existing structures do not already provide one.
4. Implement GitHub client branch ruleset fetch:
   - endpoint: `repos/{repo}/rules/branches/{branch}`;
   - parse `.[] | select(.type=="pull_request") | .parameters.allowed_merge_methods`;
   - normalize values to `merge`, `squash`, `rebase`;
   - return unconstrained when there is no applicable pull_request rule or the endpoint returns an empty list.
5. Implement effective method selection in the runner:
   - read repo-level flags from existing repo metadata path or add a narrow client method if needed;
   - map repo flags to allowed methods;
   - if ruleset-constrained, intersect with repo-allowed;
   - if unconstrained, use repo-allowed;
   - choose preferred method from existing/default preference, preserving `squash` when allowed.
6. Pass the chosen method explicitly to `merge_pr`.
7. Classify permanent method-disallowed errors using specific `GitHubClientError` messages for squash, merge commit, and rebase rejection.
8. On method-disallowed rejection:
   - record the same merge failure event/audit payload currently recorded;
   - remove the rejected method from candidates;
   - retry once with the next effective-allowed method if present;
   - otherwise transition through existing `NotifyHuman` with a merge-method mismatch reason.
9. Keep transient merge failures on the existing retry path.
10. Verify externally merged PR short-circuit behavior and make only minimal changes if missing.

## Validation Commands

Run focused checks only during the agent phase, per the AWF workspace contract. Do not run full repository gates, full coverage gates, full frontend builds, or CI-equivalent validation unless explicitly requested later by the operator.

Planned focused commands after implementation:

```bash
uv run --python 3.12 --extra dev pytest <focused test file(s)> -q
uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py src/awf/runtime/pr_monitor_runner/merge_loop.py <focused test file(s)>
uv run --python 3.12 --extra dev mypy src/awf/common/github_client.py src/awf/runtime/pr_monitor_runner/merge_loop.py
```

If `src/awf/runtime/pr_monitor.py` is touched, include it in focused ruff and mypy commands.

Record focused validation evidence in `plans/ISSUE_352_VALIDATION.md`, along with a note that full AWF/GitHub validation, provenance, timeouts, and merge gating are managed by AWF after agent completion.

The task text requests a full local gate before pushing, but the AWF workspace contract for this run explicitly forbids broad validation and pushing during the agent phase. The narrower AWF contract takes precedence for local execution in this workspace.

## Risks and Assumptions

- Assumption: the GitHub CLI endpoint `repos/{repo}/rules/branches/{branch}` is available in the installed `gh` version and returns the branch-effective ruleset shape described in the issue.
- Assumption: existing repo metadata fetching already exposes or can be narrowly extended to expose `allow_merge_commit`, `allow_squash_merge`, and `allow_rebase_merge`.
- Risk: branch names may contain slashes or special characters; endpoint construction must follow existing client escaping/argument patterns.
- Risk: multiple pull_request rules may be returned. The implementation must define deterministic behavior, likely treating any returned explicit method list as constraints and combining them conservatively according to GitHub's effective response semantics.
- Risk: GraphQL rejection messages may vary. Classification should cover known strings for squash, merge commits, and rebase without broadly treating every GraphQL failure as permanent.
- Risk: an empty effective method set could come from inconsistent repo/ruleset configuration or inability to resolve metadata. That must escalate clearly instead of retrying forever.
- Risk: adding method resolution to the runner may increase I/O during monitor polls. Keep calls scoped to the merge attempt path, not every polling decision, unless existing architecture already caches PR metadata there.
- Risk: preserving event/audit reason codes while adding NotifyHuman requires care so observability remains compatible with existing consumers.

## Explicit Non-Goals

- Do not change AWF branch management, push behavior, PR creation, or monitor ownership boundaries.
- Do not hard-code `main`, `development`, `dimileeh/aira-agent`, `dimileeh/aira-web`, or any project-specific repository behavior.
- Do not introduce repo-level merge flags as the only source of truth; base-branch rulesets must be consulted.
- Do not refactor the pure `pr_monitor.decide()` core into doing GitHub I/O.
- Do not broaden retry behavior or hide failures behind generic retries.
- Do not run broad validation, full coverage gates, full frontend builds, pushes, rebases, or commits in this AWF agent phase.
- Do not regenerate `openapi.json` unless implementation unexpectedly changes schemas.
